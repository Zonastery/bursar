"""Integration tests for PostgresBillingStore + BillingService — mirrors
JavaScript tests/billing-integration.test.ts.

Tests sync/resolve round-trips, customer/subscription CRUD, event
idempotency, topup credits, and the full subscription lifecycle against a
compatible PostgreSQL database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import psycopg2
import psycopg2.pool
import pytest

from bursar.billing.billing_service import BillingService
from bursar.billing.contracts import (
    BillingDisputeUpsert,
    BillingInvoiceUpsert,
    BillingPaymentUpsert,
    CheckoutIntentCreate,
)
from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.types import (
    BillingCustomerInfo,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventType,
    BillingInvoiceInfo,
    BillingPaymentInfo,
    BillingPreferences,
    BillingRefundInfo,
    BillingSubscriptionChangeInput,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.credits.service import CreditsService
from bursar.errors import StoreError
from tests.conftest import TEST_TENANT_ID
from tests.dodo_fixtures import map_dodo_event

pytestmark = [pytest.mark.integration]

USER_ID = "00000000-0000-0000-0000-000000000001"
USER_ID2 = "00000000-0000-0000-0000-000000000002"
USER_ID3 = "00000000-0000-0000-0000-000000000003"
USER_ID4 = "00000000-0000-0000-0000-000000000004"
USER_ID5 = "00000000-0000-0000-0000-000000000005"
PROVIDER = "stripe"
CUSTOMER_ID = "cus_test123"
CUSTOMER_ID2 = "cus_test456"
SUB_ID = "sub_test789"
SUB_ID2 = "sub_test012"
PRODUCT_ID = "prod_monthly"
PRICE_ID = "price_monthly_1000"
PRICE_ID_TOPUP = "price_topup_credits"
EVENT_ID = "evt_test_001"
DODO_PRODUCT_ID = "prod_dodo_monthly"

PRICING_DICT = {
    "version": 1,
    "catalog": {"default_plan": "free"},
    "pricing": {
        "operations": {
            "inference": {
                "measures": {"tokens": {"unit": "token"}},
                "dimensions": {},
            }
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "inference": {
                        "rules": [],
                        "unmatched": {
                            "action": "charge",
                            "charge": {
                                "type": "per_unit",
                                "measure": "tokens",
                                "rate": "1",
                            },
                        },
                    }
                }
            }
        },
    },
    "credits": {
        "buckets": {
            "purchased": {
                "priority": 10,
                "expiry": {"type": "never"},
            }
        },
        "default_bucket": "purchased",
    },
    "plans": {
        "free": {
            "display_name": "Free",
            "rank": 0,
            "rate_card": "standard",
            "credit_allowance": {
                "amount": "1000",
                "priority": 5,
                "window": {
                    "type": "calendar",
                    "unit": "month",
                    "count": 1,
                    "timezone": "UTC",
                },
            },
        },
        "pro": {
            "display_name": "Pro",
            "rank": 1,
            "rate_card": "standard",
            "credit_allowance": {
                "amount": "100000",
                "priority": 5,
                "window": {
                    "type": "calendar",
                    "unit": "month",
                    "count": 1,
                    "timezone": "UTC",
                },
            },
        },
        "enterprise": {
            "display_name": "Enterprise",
            "rank": 2,
            "rate_card": "standard",
            "credit_allowance": {
                "amount": "1000000",
                "priority": 5,
                "window": {
                    "type": "calendar",
                    "unit": "month",
                    "count": 1,
                    "timezone": "UTC",
                },
            },
        },
    },
    "commerce": {
        "providers": {
            "stripe": {"type": "stripe"},
            "dodo": {"type": "dodo"},
        },
        "offers": {
            "pro_monthly": {
                "type": "subscription",
                "display_name": "Pro Monthly",
                "price": {
                    "amount_minor": 1000,
                    "currency": "USD",
                },
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_monthly_1000",
                    },
                    "dodo": {
                        "type": "dodo_product",
                        "product_id": "prod_dodo_monthly",
                    },
                },
                "plan": "pro",
                "billing_interval": {"unit": "month", "count": 1},
            },
            "enterprise_yearly": {
                "type": "subscription",
                "display_name": "Enterprise Yearly",
                "price": {
                    "amount_minor": 10000,
                    "currency": "USD",
                },
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_yearly_10000",
                    }
                },
                "plan": "enterprise",
                "billing_interval": {"unit": "year", "count": 1},
            },
            "cycle_grant_monthly": {
                "type": "subscription",
                "display_name": "Cycle Grant Monthly",
                "price": {
                    "amount_minor": 5000,
                    "currency": "USD",
                },
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_cycle_grant_5000",
                    }
                },
                "plan": "pro",
                "billing_interval": {"unit": "month", "count": 1},
                "cycle_grant": {
                    "amount": "5000",
                    "bucket": "purchased",
                    "renewal": "replace_previous",
                    "expiry": {"type": "subscription_end"},
                },
            },
            "standard_topup": {
                "type": "topup",
                "display_name": "Standard Top-up",
                "price": {
                    "amount_minor": 1000,
                    "currency": "USD",
                },
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_topup_credits",
                    }
                },
                "credits_per_unit": "1000",
                "bucket": "purchased",
                "quantity": {"minimum": 1, "maximum": 100, "default": 1},
            },
        },
    },
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bind_tenant(cursor: psycopg2.extensions.cursor) -> None:
    cursor.execute(
        "SELECT set_config('bursar.tenant_id', %s, true)",
        (TEST_TENANT_ID,),
    )
    cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")


_COMPONENT_STORES: list[PostgresBillingStore] = []


@pytest.fixture(autouse=True)
def _close_component_stores() -> Iterator[None]:
    try:
        yield
    finally:
        while _COMPONENT_STORES:
            _COMPONENT_STORES.pop().close()


def _make_components(
    pg_database_url: str,
    pg_store: object,
) -> tuple[PostgresBillingStore, CreditsService, BillingService]:
    bs = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        provider_environment="test",
    )
    _COMPONENT_STORES.append(bs)
    cm = CreditsService(store=pg_store)  # type: ignore[arg-type]
    cm.publish_and_activate_catalog(PRICING_DICT)
    sink = BillingService(bs, provisioning=cm)
    return bs, cm, sink


# ── Sync + Resolve ─────────────────────────────────────────────────────


class TestBillingSync:
    def test_config_sync_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        offer = bs.resolve_billing_offer(PROVIDER, product_id=None, price_id=PRICE_ID)
        assert offer is not None
        assert offer.offer_key == "pro_monthly"
        assert offer.plan == "pro"

    def test_config_resolves_same_offer_by_product_id(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        offer = bs.resolve_billing_offer("dodo", product_id=DODO_PRODUCT_ID, price_id=None)
        assert offer is not None
        assert offer.offer_key == "pro_monthly"
        assert offer.plan == "pro"

    def test_topup_config_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, product_id=None, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert topup.topup_key == "standard_topup"
        assert topup.credits_per_unit == 1000

    def test_unresolved_offer_returns_null(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.resolve_billing_offer(PROVIDER, product_id=None, price_id="nonexistent") is None

    def test_resolve_billing_offer_no_match(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.resolve_billing_offer("nonexistent_provider", product_id=None, price_id=PRICE_ID) is None


# ── Checkout intent idempotency ───────────────────────────────────────


class TestCheckoutIntentIdempotency:
    def test_terminal_checkout_replay_does_not_reopen_provider_attempt(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _make_components(pg_database_url, pg_store)
        first_expiry = datetime.now(UTC) + timedelta(hours=1)
        retry_expiry = first_expiry + timedelta(hours=1)
        digest = "11" * 32

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'),
                    %s::timestamptz, %s, %s
                )
                """,
                (
                    USER_ID,
                    PROVIDER,
                    "checkout-terminal-replay",
                    "subscription",
                    "pro_monthly",
                    digest,
                    first_expiry,
                    "session-original",
                    "https://example.test/original",
                ),
            )
            intent_id = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            cursor.execute(
                "SELECT bursar.advance_checkout_intent(%s::uuid, 'failed', NULL, NULL)",
                (intent_id,),
            )
            assert cursor.fetchone() == (True,)

            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'),
                    %s::timestamptz, %s, %s
                )
                """,
                (
                    USER_ID,
                    PROVIDER,
                    "checkout-terminal-replay",
                    "subscription",
                    "pro_monthly",
                    digest,
                    retry_expiry,
                    "session-retry",
                    "https://example.test/retry",
                ),
            )
            assert cursor.fetchone() == (intent_id,)

            cursor.execute(
                """
                SELECT bursar.advance_checkout_intent(
                    %s::uuid, NULL, %s, %s
                )
                """,
                (intent_id, "session-terminal-attach", "https://example.test/terminal-attach"),
            )
            assert cursor.fetchone() == (False,)

            cursor.execute(
                """
                SELECT status, provider_session_id, checkout_url, expires_at
                FROM bursar.billing_checkout_intents
                WHERE id = %s
                """,
                (intent_id,),
            )
            assert cursor.fetchone() == (
                "failed",
                "session-original",
                "https://example.test/original",
                first_expiry,
            )

    def test_checkout_replay_preserves_a_stale_operation_and_a_new_key_creates_a_fresh_intent(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _make_components(pg_database_url, pg_store)
        digest = "22" * 32
        retry_expiry = datetime.now(UTC) + timedelta(hours=2)

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz,
                    %s, %s
                )
                """,
                (
                    USER_ID2,
                    PROVIDER,
                    "checkout-stale-replay",
                    "subscription",
                    "pro_monthly",
                    digest,
                    datetime.now(UTC) + timedelta(hours=1),
                    "session-stale",
                    "https://example.test/stale",
                ),
            )
            intent_id = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            cursor.execute(
                """
                UPDATE bursar.billing_checkout_intents
                SET created_at = now() - interval '2 hours',
                    expires_at = now() - interval '1 hour'
                WHERE id = %s
                RETURNING status, provider_session_id, checkout_url, expires_at
                """,
                (intent_id,),
            )
            stale_row = cursor.fetchone()
            assert stale_row is not None
            assert stale_row[:3] == (
                "open",
                "session-stale",
                "https://example.test/stale",
            )
            assert stale_row[3] < datetime.now(UTC)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz
                )
                """,
                (
                    USER_ID2,
                    PROVIDER,
                    "checkout-stale-replay",
                    "subscription",
                    "pro_monthly",
                    digest,
                    retry_expiry,
                ),
            )
            assert cursor.fetchone() == (intent_id,)
            cursor.execute(
                """
                SELECT status, provider_session_id, checkout_url, expires_at
                FROM bursar.billing_checkout_intents
                WHERE id = %s
                """,
                (intent_id,),
            )
            assert cursor.fetchone() == stale_row

            cursor.execute(
                "SELECT bursar.advance_checkout_intent(%s::uuid, 'expired', NULL, NULL)",
                (intent_id,),
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz
                )
                """,
                (
                    USER_ID2,
                    PROVIDER,
                    "checkout-stale-replacement",
                    "subscription",
                    "pro_monthly",
                    digest,
                    retry_expiry,
                ),
            )
            fresh_intent_id = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            assert fresh_intent_id != intent_id
            cursor.execute(
                """
                SELECT status, provider_session_id, checkout_url, expires_at
                FROM bursar.billing_checkout_intents
                WHERE id = %s
                """,
                (fresh_intent_id,),
            )
            assert cursor.fetchone() == ("open", None, None, retry_expiry)


# ── Auto-recharge profile ─────────────────────────────────────────────


class TestAutoRechargeProfile:
    def test_eligible_projected_topup_can_enable_auto_recharge(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        config = deepcopy(PRICING_DICT)
        config["commerce"]["auto_recharge"] = {
            "eligible_topups": ["standard_topup"],
            "balance_below": {
                "minimum": "100",
                "maximum": "5000",
                "default": "1000",
            },
            "rearm_above": "6000",
            "quantity": {"minimum": 1, "maximum": 3, "default": 1},
            "limits": {
                "max_purchases": 3,
                "window": {
                    "type": "rolling",
                    "duration": {"unit": "day", "count": 30},
                },
                "max_charge_minor": 3000,
                "cooldown": {"unit": "hour", "count": 1},
            },
        }
        CreditsService(store=pg_store).publish_and_activate_catalog(config)  # type: ignore[arg-type]

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT topup.id
                FROM bursar.catalog_topups AS topup
                JOIN bursar.catalog_revisions AS revision
                  ON revision.id = topup.catalog_revision_id
                 AND revision.status = 'active'
                WHERE topup.topup_key = 'standard_topup'
                """
            )
            topup_id = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            cursor.execute(
                """
                SELECT bursar.upsert_auto_recharge_profile(
                    %s::uuid, true, %s, %s::uuid, 1, 1000,
                    3, 'day', 30, 'rolling', 'UTC'
                )
                """,
                (USER_ID, PROVIDER, topup_id),
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                """
                SELECT enabled, provider, topup_id
                FROM bursar.billing_auto_recharge_profiles
                WHERE subject_id = %s::uuid
                """,
                (USER_ID,),
            )
            assert cursor.fetchone() == (True, PROVIDER, topup_id)

    def test_attempt_claims_are_isolated_by_subject_and_environment(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _make_components(pg_database_url, pg_store)

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT topup.id, topup.catalog_revision_id
                FROM bursar.catalog_topups AS topup
                JOIN bursar.catalog_revisions AS revision
                  ON revision.id = topup.catalog_revision_id
                 AND revision.status = 'active'
                WHERE topup.topup_key = 'standard_topup'
                """
            )
            topup_id, revision_id = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
            cursor.execute(
                """
                INSERT INTO bursar.subjects(id)
                VALUES (%s::uuid), (%s::uuid)
                ON CONFLICT (tenant_id, id) DO NOTHING
                """,
                (USER_ID, USER_ID2),
            )
            cursor.execute(
                """
                INSERT INTO bursar.billing_auto_recharge_attempts(
                    subject_id,
                    provider,
                    provider_environment,
                    idempotency_key,
                    topup_id,
                    catalog_revision_id,
                    quantity,
                    window_start,
                    window_end
                )
                VALUES
                    (
                        %s::uuid, %s, 'live', 'live-user-1',
                        %s::uuid, %s::uuid, 1, now(), now() + interval '1 day'
                    ),
                    (
                        %s::uuid, %s, 'live', 'live-user-2',
                        %s::uuid, %s::uuid, 1, now(), now() + interval '1 day'
                    ),
                    (
                        %s::uuid, %s, 'test', 'test-user-1',
                        %s::uuid, %s::uuid, 1, now(), now() + interval '1 day'
                    )
                """,
                (
                    USER_ID,
                    PROVIDER,
                    topup_id,
                    revision_id,
                    USER_ID2,
                    PROVIDER,
                    topup_id,
                    revision_id,
                    USER_ID,
                    PROVIDER,
                    topup_id,
                    revision_id,
                ),
            )
            cursor.execute(
                """
                SELECT provider_environment, count(*)
                FROM bursar.billing_auto_recharge_attempts
                GROUP BY provider_environment
                ORDER BY provider_environment
                """
            )
            assert cursor.fetchall() == [("live", 2), ("test", 1)]

    def test_entitlement_selection_is_isolated_by_environment(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _make_components(pg_database_url, pg_store)

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT offer.id, offer.catalog_revision_id
                FROM bursar.catalog_offers AS offer
                JOIN bursar.catalog_revisions AS revision
                  ON revision.id = offer.catalog_revision_id
                 AND revision.status = 'active'
                WHERE offer.offer_key = 'pro_monthly'
                """
            )
            offer_id, revision_id = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
            cursor.execute(
                """
                INSERT INTO bursar.subjects(id)
                VALUES (%s::uuid)
                ON CONFLICT (tenant_id, id) DO NOTHING
                """,
                (USER_ID,),
            )
            cursor.execute(
                """
                INSERT INTO bursar.billing_subscriptions(
                    subject_id,
                    provider,
                    provider_environment,
                    provider_subscription_id,
                    offer_id,
                    catalog_revision_id,
                    status,
                    provider_updated_at,
                    status_changed_at
                )
                VALUES
                    (
                        %s::uuid, %s, 'live', 'sub-live',
                        %s::uuid, %s::uuid, 'active',
                        '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z'
                    ),
                    (
                        %s::uuid, %s, 'test', 'sub-test',
                        %s::uuid, %s::uuid, 'active',
                        '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z'
                    )
                RETURNING id, provider_environment
                """,
                (
                    USER_ID,
                    PROVIDER,
                    offer_id,
                    revision_id,
                    USER_ID,
                    PROVIDER,
                    offer_id,
                    revision_id,
                ),
            )
            subscription_ids = {environment: subscription_id for subscription_id, environment in cursor.fetchall()}

            for environment in ("live", "test"):
                cursor.execute(
                    "SELECT set_config('bursar.provider_environment', %s, false)",
                    (environment,),
                )
                cursor.execute(
                    "SELECT bursar.select_entitlement_source(%s::uuid, %s::uuid)",
                    (USER_ID, subscription_ids[environment]),
                )
                assert cursor.fetchone() == (True,)

            cursor.execute(
                """
                SELECT provider_environment
                FROM bursar.billing_entitlement_sources
                WHERE subject_id = %s::uuid
                  AND selected
                ORDER BY provider_environment
                """,
                (USER_ID,),
            )
            assert cursor.fetchall() == [("live",), ("test",)]


# ── Customer CRUD ─────────────────────────────────────────────────────


class TestCustomerCrud:
    def test_customer_created_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com")
        uid = bs.get_billing_customer(PROVIDER, CUSTOMER_ID)
        assert uid == USER_ID

    def test_customer_not_found(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.get_billing_customer(PROVIDER, "nonexistent_cus") is None

    def test_customer_remap_to_different_user_rejected(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID)
        with pytest.raises(StoreError) as failure:
            bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID2)
        assert failure.value.details is not None
        assert failure.value.details["sql_state"] == "23505"
        assert isinstance(failure.value.cause, psycopg2.errors.UniqueViolation)
        assert bs.get_billing_customer(PROVIDER, CUSTOMER_ID) == USER_ID

    def test_multiple_providers_same_customer_id(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer("stripe", CUSTOMER_ID, USER_ID)
        bs.upsert_billing_customer("dodo", CUSTOMER_ID, USER_ID2)
        assert bs.get_billing_customer("stripe", CUSTOMER_ID) == USER_ID
        assert bs.get_billing_customer("dodo", CUSTOMER_ID) == USER_ID2


# ── Subscription CRUD ─────────────────────────────────────────────────


class TestSubscriptionCrud:
    def test_subscription_upsert_and_read(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        state = BillingSubscriptionState(
            user_id=USER_ID,
            provider=PROVIDER,
            provider_subscription_id=SUB_ID,
            provider_customer_id=CUSTOMER_ID,
            offer_key="pro_monthly",
            plan="pro",
            status=BillingSubscriptionStatus.active,
            current_period_start="2025-01-01T00:00:00Z",
            current_period_end="2025-02-01T00:00:00Z",
            provider_updated_at="2025-01-01T00:00:00Z",
            cancel_at_period_end=False,
        )
        bs.upsert_billing_subscription(state)
        result = bs.get_billing_subscription(PROVIDER, SUB_ID)
        assert result is not None
        assert result.user_id == USER_ID
        assert result.status == BillingSubscriptionStatus.active
        assert result.plan == "pro"

    def test_subscription_not_found(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.get_billing_subscription(PROVIDER, "nonexistent_sub") is None

    def test_subscription_update(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                offer_key="pro_monthly",
                status=BillingSubscriptionStatus.active,
                provider_updated_at="2025-01-01T00:00:00Z",
                cancel_at_period_end=False,
            )
        )
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                status=BillingSubscriptionStatus.canceled,
                provider_updated_at="2025-01-02T00:00:00Z",
                cancel_at_period_end=False,
            )
        )
        sub = bs.get_billing_subscription(PROVIDER, SUB_ID)
        assert sub is not None
        assert sub.status == BillingSubscriptionStatus.canceled


# ── Financial read models ─────────────────────────────────────────────


class TestFinancialReadModels:
    def test_invoice_dispute_preferences_and_customer_views_share_one_persisted_account(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, _cm, service = _make_components(pg_database_url, pg_store)
        missing_user = "00000000-0000-0000-0000-000000000099"
        assert service.get_user_preferences(missing_user) is None
        assert service.list_billing_invoices(missing_user) == []
        assert service.get_customer_by_user_id(missing_user) is None

        with pytest.raises(StoreError, match="invoice subscription is not available"):
            bs.upsert_billing_invoice(
                BillingInvoiceUpsert(
                    provider=PROVIDER,
                    provider_invoice_id="in_missing_subscription",
                    provider_subscription_id="sub_missing",
                    user_id=USER_ID,
                    status="open",
                    amount_paid_minor=0,
                    amount_due_minor=1000,
                    currency="USD",
                    provider_updated_at="2026-08-19T10:00:00Z",
                )
            )
        with pytest.raises(StoreError, match="dispute payment is required"):
            bs.upsert_billing_dispute(
                BillingDisputeUpsert(
                    provider=PROVIDER,
                    provider_dispute_id="dp_missing_payment",
                    provider_payment_id="pay_missing",
                    status="needs_response",
                    provider_updated_at="2026-08-19T10:00:00Z",
                )
            )

        bs.upsert_billing_customer(PROVIDER, "cus_financial_views", USER_ID, "billing@example.com")
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id="sub_financial_views",
                provider_customer_id="cus_financial_views",
                offer_key="pro_monthly",
                plan="pro",
                status=BillingSubscriptionStatus.active,
                provider_updated_at="2026-08-19T10:01:00Z",
                cancel_at_period_end=False,
            )
        )
        bs.upsert_billing_payment(
            BillingPaymentUpsert(
                provider=PROVIDER,
                provider_payment_id="pay_financial_views",
                provider_invoice_id="in_financial_views",
                user_id=USER_ID,
                amount_minor=1000,
                tax_minor=100,
                currency="USD",
                purpose="subscription",
                status="succeeded",
                provider_updated_at="2026-08-19T10:02:00Z",
                metadata={"reconciliation_source": "integration"},
            )
        )
        bs.upsert_billing_invoice(
            BillingInvoiceUpsert(
                provider=PROVIDER,
                provider_invoice_id="in_financial_views",
                provider_subscription_id="sub_financial_views",
                user_id=USER_ID,
                status="paid",
                amount_paid_minor=1000,
                amount_due_minor=1000,
                currency="USD",
                period_start="2026-08-01T00:00:00Z",
                period_end="2026-09-01T00:00:00Z",
                provider_updated_at="2026-08-19T10:03:00Z",
            )
        )
        bs.upsert_billing_dispute(
            BillingDisputeUpsert(
                provider=PROVIDER,
                provider_dispute_id="dp_financial_views",
                provider_payment_id="pay_financial_views",
                status="needs_response",
                reason="fraudulent",
                metadata={"case": "case-1"},
                provider_updated_at="2026-08-19T10:04:00Z",
            )
        )
        preferences = BillingPreferences(
            user_id=USER_ID,
            auto_recharge=True,
            overage_protection=True,
            email_notifications=False,
            usage_alerts=True,
            invoice_reminders=False,
        )
        service.update_user_preferences(preferences)

        customer = service.get_customer_by_user_id(USER_ID, PROVIDER)
        assert customer is not None
        assert customer.provider_customer_id == "cus_financial_views"
        invoices = service.list_billing_invoices(USER_ID)
        assert [(invoice.provider_invoice_id, invoice.status) for invoice in invoices] == [
            ("in_financial_views", "paid")
        ]
        assert service.get_user_preferences(USER_ID) == preferences
        catalog = service.get_active_catalog_document()
        assert catalog is not None
        assert catalog["version"] == 1
        assert catalog["commerce"]["offers"]["pro_monthly"]["plan"] == "pro"
        payment = bs.get_billing_payment(PROVIDER, "pay_financial_views")
        assert payment is not None
        assert payment.metadata == {"reconciliation_source": "integration"}

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT status, reason, metadata
                FROM bursar.billing_disputes
                WHERE provider = %s AND provider_dispute_id = %s
                """,
                (PROVIDER, "dp_financial_views"),
            )
            assert cursor.fetchone() == ("needs_response", "fraudulent", {"case": "case-1"})

    def test_failed_payment_grace_recovers_through_invoice_and_dispute_events(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, credits, _service = _make_components(pg_database_url, pg_store)
        service = BillingService(bs, provisioning=credits, past_due_grace_period_ms=1000)
        customer = BillingCustomerInfo(provider_customer_id="cus_recovery")
        subscription_id = "sub_recovery"
        refs = ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID)

        created = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_recovery_subscription",
                event_type=BillingEventType.subscription_created,
                occurred_at="2026-08-19T10:00:00Z",
                account_id=USER_ID5,
                customer=customer,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    period_start="2026-08-01T00:00:00Z",
                    period_end="2026-09-01T00:00:00Z",
                    refs=refs,
                ),
            )
        )
        assert created.action == "subscription_created"
        assert credits.get_user_plan(USER_ID5).plan_id is not None

        failed = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_recovery_payment_failed",
                event_type=BillingEventType.payment_failed,
                occurred_at="2026-08-19T10:01:00Z",
                customer=customer,
                subscription=BillingSubscriptionInfo(provider_subscription_id=subscription_id),
                payment=BillingPaymentInfo(
                    provider_payment_id="pay_recovery",
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    purpose="subscription",
                    status="failed",
                ),
            )
        )
        assert failed.action == "payment_failed_recorded"
        past_due = bs.get_billing_subscription(PROVIDER, subscription_id)
        assert past_due is not None
        assert past_due.status == BillingSubscriptionStatus.past_due
        assert past_due.grace_ends_at is not None
        assert credits.get_user_plan(USER_ID5).plan_id is not None

        assert service.expire_past_due_grace_periods(datetime.fromisoformat("2026-08-19T10:02:00+00:00")) == 1
        assert credits.get_user_plan(USER_ID5).plan_id is None
        assert service.expire_past_due_grace_periods(datetime.fromisoformat("2026-08-19T10:02:00+00:00")) == 0

        paid = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_recovery_invoice_paid",
                event_type=BillingEventType.invoice_paid,
                occurred_at="2026-08-19T10:03:00Z",
                customer=customer,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    period_start="2026-08-19T10:03:00Z",
                    period_end="2026-09-19T10:03:00Z",
                    refs=refs,
                ),
                invoice=BillingInvoiceInfo(
                    provider_invoice_id="in_recovery",
                    status="paid",
                    amount_paid_minor=1000,
                    amount_due_minor=1000,
                    currency="USD",
                    period_start="2026-08-19T10:03:00Z",
                    period_end="2026-09-19T10:03:00Z",
                ),
            )
        )
        assert paid.action == "subscription_renewed"
        assert credits.get_user_plan(USER_ID5).plan_id is not None
        assert [invoice.provider_invoice_id for invoice in service.list_billing_invoices(USER_ID5)] == ["in_recovery"]

        dispute_events = (
            (
                BillingEvent(
                    provider=PROVIDER,
                    event_id="evt_recovery_dispute_created",
                    event_type=BillingEventType.dispute_created,
                    occurred_at="2026-08-19T10:04:00Z",
                    customer=customer,
                    dispute=BillingDisputeInfo(
                        provider_dispute_id="dp_recovery",
                        provider_payment_id="pay_recovery",
                        status="needs_response",
                        reason="fraudulent",
                    ),
                ),
                "dispute_recorded",
            ),
            (
                BillingEvent(
                    provider=PROVIDER,
                    event_id="evt_recovery_dispute_closed",
                    event_type=BillingEventType.dispute_closed,
                    occurred_at="2026-08-19T10:05:00Z",
                    customer=customer,
                    dispute=BillingDisputeInfo(
                        provider_dispute_id="dp_recovery",
                        provider_payment_id="pay_recovery",
                        status="won",
                        reason="won",
                    ),
                ),
                "dispute_closed",
            ),
        )
        for dispute_event, action in dispute_events:
            result = service.ingest_billing_event(dispute_event)
            assert result.action == action

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT status, reason
                FROM bursar.billing_disputes
                WHERE provider = %s AND provider_dispute_id = %s
                """,
                (PROVIDER, "dp_recovery"),
            )
            assert cursor.fetchone() == ("won", "won")

    def test_subscription_payment_reconciles_invoice_and_async_callback(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, credits, _sink = _make_components(pg_database_url, pg_store)
        callback_events: list[str] = []

        async def on_payment_succeeded(event: BillingEvent, user_id: str) -> None:
            await asyncio.sleep(0)
            callback_events.append(f"{event.event_id}:{user_id}")

        service = BillingService(
            bs,
            provisioning=credits,
            event_handlers={BillingEventType.payment_succeeded: on_payment_succeeded},
        )
        subscription_id = "sub_payment_reconciliation"
        refs = ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID)
        created = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_payment_reconciliation_created",
                event_type=BillingEventType.subscription_created,
                occurred_at="2026-08-19T10:00:00Z",
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_payment_reconciliation"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    period_start="2026-08-01T00:00:00Z",
                    period_end="2026-09-01T00:00:00Z",
                    refs=refs,
                ),
            )
        )
        assert created.action == "subscription_created"
        assert service.has_provisioning is True
        assert service.get_active_subscription(USER_ID) is not None
        assert service.list_cancellable_provider_subscription_ids(USER_ID) == [subscription_id]

        intent = service.create_or_get_checkout_intent(
            CheckoutIntentCreate(
                subject_id=USER_ID,
                provider=PROVIDER,
                operation_key="subscription-payment-reconciliation",
                checkout_kind="subscription",
                product_key="pro_monthly",
                request_digest="33" * 32,
                expires_at="2026-08-20T00:00:00Z",
            )
        )
        payment_event = BillingEvent(
            provider=PROVIDER,
            event_id="evt_payment_reconciliation_succeeded",
            event_type=BillingEventType.payment_succeeded,
            occurred_at="2026-08-19T10:01:00Z",
            subscription=BillingSubscriptionInfo(
                provider_subscription_id=subscription_id,
                period_start="2026-08-19T10:01:00Z",
                period_end="2026-09-19T10:01:00Z",
            ),
            payment=BillingPaymentInfo(
                provider_payment_id="pay_subscription_reconciliation",
                amount_minor=1000,
                tax_minor=100,
                currency="USD",
                purpose="subscription",
                status="succeeded",
            ),
            metadata={"checkout_intent_id": intent.id},
        )

        async def ingest_payment():
            return service.ingest_billing_event(payment_event)

        result = asyncio.run(ingest_payment())
        assert result.action == "payment_succeeded"
        assert callback_events == [f"evt_payment_reconciliation_succeeded:{USER_ID}"]
        payment = bs.get_billing_payment(PROVIDER, "pay_subscription_reconciliation")
        assert payment is not None
        assert payment.amount_minor == 1000
        assert payment.tax_minor == 100
        invoices = service.list_billing_invoices(USER_ID)
        assert [(invoice.provider_invoice_id, invoice.status) for invoice in invoices] == [
            ("pay_subscription_reconciliation", "paid")
        ]
        completed = service.get_checkout_intent(intent.id, USER_ID)
        assert completed is not None
        assert completed.status == "completed"

    def test_failed_subscription_event_is_durable_and_retryable(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bs, _credits, service = _make_components(pg_database_url, pg_store)
        service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_grace_read_created",
                event_type=BillingEventType.subscription_created,
                occurred_at="2026-08-19T10:01:00Z",
                account_id=USER_ID2,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_grace_read",
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_grace_read_failed",
                event_type=BillingEventType.payment_failed,
                occurred_at="2026-08-19T10:02:00Z",
                account_id=USER_ID2,
                subscription=BillingSubscriptionInfo(provider_subscription_id="sub_grace_read"),
                payment=BillingPaymentInfo(
                    provider_payment_id="pay_grace_read",
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    purpose="subscription",
                    status="failed",
                ),
            )
        )
        assert service.get_user_subscription(USER_ID2) is not None
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                UPDATE bursar.billing_subscriptions
                SET grace_ends_at = now() - interval '1 minute'
                WHERE provider = %s AND provider_subscription_id = %s
                """,
                (PROVIDER, "sub_grace_read"),
            )
        expired = service.get_user_subscription(USER_ID2)
        assert expired is not None
        assert expired.grace_expired_at is not None
        missing_account = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_missing_account_cancellation",
                event_type=BillingEventType.subscription_canceled,
                occurred_at="2026-08-19T10:02:30Z",
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_missing_account_cancellation",
                    status=BillingSubscriptionStatus.canceled,
                ),
            )
        )
        assert missing_account.handled is False
        assert missing_account.error is not None
        event = BillingEvent(
            provider=PROVIDER,
            event_id="evt_unknown_subscription_cancellation",
            event_type=BillingEventType.subscription_canceled,
            occurred_at="2026-08-19T10:02:00Z",
            account_id=USER_ID2,
            subscription=BillingSubscriptionInfo(
                provider_subscription_id="sub_unknown_cancellation",
                status=BillingSubscriptionStatus.canceled,
            ),
        )
        first = service.ingest_billing_event(event)
        assert first.handled is False
        assert first.error is not None
        retry = service.ingest_billing_event(event)
        assert retry.handled is False
        assert retry.error is not None
        assert retry.error.startswith("billing_event_processing_failed:")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT event.status, event.attempt_count, payload.envelope->'subscription'->>'providerSubscriptionId'
                FROM bursar.billing_events AS event
                JOIN bursar.billing_event_payloads AS payload ON payload.event_id = event.id
                WHERE event.provider = %s AND event.provider_event_id = %s
                """,
                (PROVIDER, "evt_unknown_subscription_cancellation"),
            )
            assert cursor.fetchone() == ("failed", 2, "sub_unknown_cancellation")

    def test_subscription_update_preserves_access_then_expiry_uses_terminal_plan(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, credits, _sink = _make_components(pg_database_url, pg_store)
        service = BillingService(bs, provisioning=credits, terminal_plan_key="free")
        subscription_id = "sub_terminal_plan_reconciliation"
        created = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_created",
                event_type=BillingEventType.subscription_created,
                occurred_at="2026-08-19T10:03:00Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert created.handled is True
        assert credits.get_user_plan(USER_ID3).plan_key == "pro"

        yearly = service.resolve_offer(PROVIDER, price_id="price_yearly_10000")
        assert yearly is not None
        service.create_billing_subscription_change(
            BillingSubscriptionChangeInput(
                provider=PROVIDER,
                provider_subscription_id=subscription_id,
                to_offer_id=yearly.offer_id,
                effective_at="2026-09-01T00:00:00Z",
                effective="renewal",
                idempotency_key="terminal-plan-change",
            )
        )
        assert service.get_open_billing_subscription_change(PROVIDER, subscription_id) is not None
        changed = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_changed",
                event_type=BillingEventType.subscription_plan_changed,
                occurred_at="2026-08-19T10:03:30Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id="price_yearly_10000"),
                ),
            )
        )
        assert changed.action == "plan_changed"
        assert service.get_open_billing_subscription_change(PROVIDER, subscription_id) is None
        assert credits.get_user_plan(USER_ID3).plan_key == "enterprise"

        updated = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_updated",
                event_type=BillingEventType.subscription_updated,
                occurred_at="2026-08-19T10:04:00Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                ),
            )
        )
        assert updated.action == "subscription_updated"
        assert credits.get_user_plan(USER_ID3).plan_key == "enterprise"

        refreshed = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_refreshed",
                event_type=BillingEventType.subscription_updated,
                occurred_at="2026-08-19T10:04:30Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id="price_yearly_10000"),
                ),
            )
        )
        assert refreshed.action == "subscription_updated"

        paused = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_paused",
                event_type=BillingEventType.subscription_updated,
                occurred_at="2026-08-19T10:04:45Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.paused,
                ),
            )
        )
        assert paused.action == "subscription_updated"
        assert credits.get_user_plan(USER_ID3).plan_key == "free"

        expired = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_terminal_plan_expired",
                event_type=BillingEventType.subscription_expired,
                occurred_at="2026-08-19T10:05:00Z",
                account_id=USER_ID3,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.expired,
                ),
            )
        )
        assert expired.action == "subscription_expired"
        assert credits.get_user_plan(USER_ID3).plan_key == "free"
        stored = bs.get_billing_subscription(PROVIDER, subscription_id)
        assert stored is not None
        assert stored.status == BillingSubscriptionStatus.expired

    def test_refund_without_account_id_resolves_subject_from_persisted_payment(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bs, credits, service = _make_components(pg_database_url, pg_store)
        payment_id = "pay_refund_identity_fallback"
        payment = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_identity_payment",
                event_type=BillingEventType.payment_succeeded,
                occurred_at="2026-08-19T11:00:00Z",
                account_id=USER_ID4,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        assert payment.action == "payment_succeeded"
        assert credits.get_balance(USER_ID4).balance == Decimal("1000")

        refund = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_identity_fallback",
                event_type=BillingEventType.refund_created,
                occurred_at="2026-08-19T11:01:00Z",
                refund=BillingRefundInfo(
                    provider_refund_id="refund_identity_fallback",
                    provider_payment_id=payment_id,
                    amount_minor=250,
                    currency="USD",
                    status="succeeded",
                ),
            )
        )
        assert refund.action == "refund_clawback"
        assert credits.get_balance(USER_ID4).balance == Decimal("750")

    def test_checkout_webhooks_close_intents_and_reject_underpaid_topups(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bs, credits, service = _make_components(pg_database_url, pg_store)

        def create_intent(operation_key: str) -> str:
            return service.create_or_get_checkout_intent(
                CheckoutIntentCreate(
                    subject_id=USER_ID3,
                    provider=PROVIDER,
                    operation_key=operation_key,
                    checkout_kind="credit_topup",
                    product_key="standard_topup",
                    request_digest="44" * 32,
                    expires_at="2026-08-20T00:00:00Z",
                )
            ).id

        expired_id = create_intent("checkout-webhook-expired")
        expired = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_checkout_webhook_expired",
                event_type=BillingEventType.checkout_expired,
                occurred_at="2026-08-19T12:00:00Z",
                account_id=USER_ID3,
                metadata={"checkout_intent_id": expired_id},
            )
        )
        assert expired.action == "ignored"
        expired_intent = service.get_checkout_intent(expired_id, USER_ID3)
        assert expired_intent is not None
        assert expired_intent.status == "expired"

        completed_id = create_intent("checkout-webhook-completed")
        completed = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_checkout_webhook_completed",
                event_type=BillingEventType.checkout_completed,
                occurred_at="2026-08-19T12:01:00Z",
                account_id=USER_ID3,
                customer=BillingCustomerInfo(
                    provider_customer_id="cus_checkout_webhook",
                    email="checkout@example.com",
                ),
                metadata={"checkout_intent_id": completed_id},
            )
        )
        assert completed.action == "checkout_completed"
        completed_intent = service.get_checkout_intent(completed_id, USER_ID3)
        assert completed_intent is not None
        assert completed_intent.status == "completed"
        assert service.get_customer_by_user_id(USER_ID3, PROVIDER) is not None

        failed_id = create_intent("checkout-webhook-payment-failed")
        failed = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_checkout_webhook_payment_failed",
                event_type=BillingEventType.payment_failed,
                occurred_at="2026-08-19T12:02:00Z",
                account_id=USER_ID3,
                payment=BillingPaymentInfo(
                    provider_payment_id="pay_checkout_webhook_failed",
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    purpose="credit_topup",
                    status="failed",
                ),
                metadata={"checkout_intent_id": failed_id},
            )
        )
        assert failed.action == "payment_failed_recorded"
        failed_intent = service.get_checkout_intent(failed_id, USER_ID3)
        assert failed_intent is not None
        assert failed_intent.status == "failed"

        underpaid = service.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_checkout_webhook_underpaid",
                event_type=BillingEventType.payment_succeeded,
                occurred_at="2026-08-19T12:03:00Z",
                account_id=USER_ID3,
                payment=BillingPaymentInfo(
                    provider_payment_id="pay_checkout_webhook_underpaid",
                    amount_minor=999,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        assert underpaid.action == "payment_succeeded_out_of_bounds"
        assert credits.get_balance(USER_ID3).balance == Decimal("0")

    def test_pseudonymized_financial_subject_cannot_reintroduce_mutable_identity(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        store, _credits, service = _make_components(pg_database_url, pg_store)
        store.upsert_billing_customer(PROVIDER, "cus_pseudonymized", USER_ID4, "pii@example.com")
        payment = BillingPaymentUpsert(
            provider=PROVIDER,
            provider_payment_id="pay_pseudonymized",
            user_id=USER_ID4,
            amount_minor=1000,
            tax_minor=0,
            currency="USD",
            purpose="subscription",
            status="succeeded",
            provider_updated_at="2026-08-19T13:00:00Z",
            metadata={"email": "pii@example.com"},
        )
        store.upsert_billing_payment(payment)
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                INSERT INTO bursar.external_identities(subject_id, provider, external_subject)
                VALUES (%s::uuid, 'host', 'external-user-pseudonymized')
                """,
                (USER_ID4,),
            )

        service.pseudonymize_financial_subject(USER_ID4)
        store.upsert_billing_customer(
            PROVIDER,
            "cus_pseudonymized",
            USER_ID4,
            "reintroduced@example.com",
        )
        store.upsert_billing_payment(
            payment.model_copy(
                update={
                    "provider_updated_at": "2026-08-19T13:00:01Z",
                    "metadata": {"email": "reintroduced@example.com"},
                }
            )
        )

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT
                    subject.pseudonymized_at IS NOT NULL,
                    customer.email,
                    customer.metadata,
                    payment.metadata,
                    EXISTS (
                        SELECT 1
                        FROM bursar.external_identities AS identity
                        WHERE identity.subject_id = subject.id
                    )
                FROM bursar.subjects AS subject
                JOIN bursar.billing_customers AS customer ON customer.subject_id = subject.id
                JOIN bursar.billing_payments AS payment ON payment.subject_id = subject.id
                WHERE subject.id = %s::uuid
                """,
                (USER_ID4,),
            )
            assert cursor.fetchone() == (True, None, {}, {}, False)

    def test_duplicate_active_subscription_is_recorded_without_replacing_entitlement(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _store, credits, service = _make_components(pg_database_url, pg_store)
        customer = BillingCustomerInfo(provider_customer_id="cus_subscription_conflict")

        def subscription_event(event_id: str, subscription_id: str) -> BillingEvent:
            return BillingEvent(
                provider=PROVIDER,
                event_id=event_id,
                event_type=BillingEventType.subscription_created,
                occurred_at="2026-08-19T14:00:00Z",
                account_id=USER_ID2,
                customer=customer,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription_id,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(price_id=PRICE_ID),
                ),
            )

        first = service.ingest_billing_event(
            subscription_event("evt_subscription_conflict_original", "sub_conflict_original")
        )
        duplicate = service.ingest_billing_event(
            subscription_event("evt_subscription_conflict_duplicate", "sub_conflict_duplicate")
        )

        assert first.action == "subscription_created"
        assert duplicate.handled is False
        assert duplicate.error == "subscription_conflict"
        assert credits.get_user_plan(USER_ID2).plan_key == "pro"
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT
                    conflict.duplicate_provider_subscription_id,
                    subscription.provider_subscription_id,
                    event.provider_event_id
                FROM bursar.billing_subscription_conflicts AS conflict
                JOIN bursar.billing_subscriptions AS subscription
                  ON subscription.id = conflict.existing_subscription_id
                JOIN bursar.billing_events AS event
                  ON event.id = conflict.billing_event_id
                WHERE event.provider_event_id = %s
                """,
                ("evt_subscription_conflict_duplicate",),
            )
            assert cursor.fetchone() == (
                "sub_conflict_duplicate",
                "sub_conflict_original",
                "evt_subscription_conflict_duplicate",
            )


# ── Event idempotency ──────────────────────────────────────────────────


class TestEventIdempotency:
    def test_event_idempotency(self, pg_database_url: str, pg_store: object) -> None:
        _bs, _cm, sink = _make_components(pg_database_url, pg_store)
        event = BillingEvent(
            provider=PROVIDER,
            event_id=EVENT_ID,
            event_type=BillingEventType.customer_created,
            occurred_at=_now(),
            account_id=USER_ID,
            customer=BillingCustomerInfo(provider_customer_id="cus_event_idempotency"),
        )
        r1 = sink.ingest_billing_event(event)
        assert r1.handled is True
        r2 = sink.ingest_billing_event(event)
        assert r2.action == "duplicate"

    def test_event_claim_complete_fail_cycle(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        c1 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c1.status == "claimed"
        assert c1.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_claim_cycle", c1.claim_token)
        c2 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c2.status == "duplicate"

    def test_event_fail_then_reclaim(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        c1 = bs.claim_billing_event(PROVIDER, "evt_fail_retry", "test.event")
        assert c1.status == "claimed"
        assert c1.claim_token is not None
        bs.fail_billing_event(PROVIDER, "evt_fail_retry", c1.claim_token, "retryable test failure")
        c2 = bs.claim_billing_event(PROVIDER, "evt_fail_retry", "test.event")
        assert c2.status == "claimed"

    def test_invalid_event_claim_does_not_store_an_event(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)

        result = bs.claim_billing_event(PROVIDER, "evt_invalid_claim", "")

        assert result.status == "invalid_request"
        assert result.billing_event_id is None
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::integer
                FROM bursar.billing_events
                WHERE tenant_id = %s::uuid
                  AND provider = %s
                  AND provider_environment = 'test'
                  AND provider_event_id = %s
                """,
                (TEST_TENANT_ID, PROVIDER, "evt_invalid_claim"),
            )
            assert cursor.fetchone() == (0,)

    def test_idempotency_conflict_preserves_the_original_event_and_payload(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        event_id = "evt_claim_conflict"
        first = bs.claim_billing_event(PROVIDER, event_id, "test.event", {"amount": 100})
        assert first.status == "claimed"
        assert first.claim_token is not None
        assert first.billing_event_id is not None
        assert bs.complete_billing_event(PROVIDER, event_id, first.claim_token) is True

        conflict = bs.claim_billing_event(PROVIDER, event_id, "test.event", {"amount": 200})

        assert conflict.status == "idempotency_conflict"
        assert conflict.billing_event_id == first.billing_event_id
        assert conflict.claim_token is None
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.id,
                       event.status,
                       event.attempt_count,
                       (SELECT count(*)::integer
                        FROM bursar.billing_event_payloads AS payload
                        WHERE payload.event_id = event.id) AS payload_count
                FROM bursar.billing_events AS event
                WHERE event.tenant_id = %s::uuid
                  AND event.provider = %s
                  AND event.provider_environment = 'test'
                  AND event.provider_event_id = %s
                """,
                (TEST_TENANT_ID, PROVIDER, event_id),
            )
            assert cursor.fetchall() == [(first.billing_event_id, "completed", 1, 1)]

    def test_retry_exhaustion_preserves_the_terminal_event_claim(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        event_id = "evt_claim_exhausted"
        first = bs.claim_billing_event(PROVIDER, event_id, "test.event")
        assert first.status == "claimed"
        assert first.claim_token is not None
        assert first.billing_event_id is not None
        assert bs.fail_billing_event(PROVIDER, event_id, first.claim_token, "attempt 1") is True

        for attempt in range(2, 4):
            retry = bs.claim_billing_event(PROVIDER, event_id, "test.event")
            assert retry.status == "claimed"
            assert retry.claim_token is not None
            assert retry.billing_event_id == first.billing_event_id
            assert bs.fail_billing_event(PROVIDER, event_id, retry.claim_token, f"attempt {attempt}") is True

        exhausted = bs.claim_billing_event(PROVIDER, event_id, "test.event")

        assert exhausted.status == "max_retries_exceeded"
        assert exhausted.billing_event_id == first.billing_event_id
        assert exhausted.claim_token is None
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.id,
                       event.status,
                       event.attempt_count,
                       (SELECT count(*)::integer
                        FROM bursar.billing_event_payloads AS payload
                        WHERE payload.event_id = event.id) AS payload_count
                FROM bursar.billing_events AS event
                WHERE event.tenant_id = %s::uuid
                  AND event.provider = %s
                  AND event.provider_environment = 'test'
                  AND event.provider_event_id = %s
                """,
                (TEST_TENANT_ID, PROVIDER, event_id),
            )
            assert cursor.fetchall() == [(first.billing_event_id, "failed", 3, 1)]

    @pytest.mark.concurrency
    def test_concurrent_event_claims_admit_one_worker(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        barrier = Barrier(12)

        def claim(_: int):
            pool = psycopg2.pool.ThreadedConnectionPool(1, 1, pg_database_url)
            local = PostgresBillingStore(
                tenant_id=TEST_TENANT_ID,
                provider_environment="test",
                pool=pool,
            )
            try:
                barrier.wait(timeout=30)
                return local.claim_billing_event(PROVIDER, "evt_concurrent_claim", "test.event")
            finally:
                local.close()
                pool.closeall()

        with ThreadPoolExecutor(max_workers=12) as executor:
            claims = list(executor.map(claim, range(12)))
        assert sum(c.status == "claimed" for c in claims) == 1
        assert sum(c.status == "busy" for c in claims) == 11
        winner = next(c for c in claims if c.status == "claimed")
        assert winner.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_concurrent_claim", winner.claim_token)
        assert bs.claim_billing_event(PROVIDER, "evt_concurrent_claim", "test.event").status == "duplicate"

    def test_event_handler_dispatched_for_matching_event(self, pg_database_url: str, pg_store: object) -> None:
        """Mirrors JavaScript test: eventHandlers dispatch on matching event type."""
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)

        called = False

        def handler(event: BillingEvent, user_id: str) -> None:
            nonlocal called
            called = True

        sink = BillingService(
            bs,
            provisioning=_cm,
            event_handlers={
                BillingEventType.subscription_trial_will_end: handler,
            },
        )
        result = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_handler_test",
                event_type=BillingEventType.subscription_trial_will_end,
                occurred_at=_now(),
                account_id=USER_ID,
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_handler_test",
                ),
            ),
        )
        assert result.handled is True
        assert called is True


# ── Topup credits ──────────────────────────────────────────────────────


class TestTopup:
    def test_compute_topup_credits(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert bs.compute_topup_credits(2000, topup) == 2000

    def test_compute_topup_credits_odd_amount(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert bs.compute_topup_credits(1999, topup) == 0


# ── BillingService lifecycle ───────────────────────────────────────────────


class TestBillingServiceLifecycle:
    def test_customer_deletion_revokes_entitlement_and_isolates_callback_failure(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        bs, cm, _sink = _make_components(pg_database_url, pg_store)
        callback_events: list[str] = []

        def failing_callback(event: BillingEvent, account_id: str) -> None:
            callback_events.append(f"{event.event_type.value}:{account_id}")
            raise RuntimeError("downstream callback unavailable")

        sink = BillingService(
            bs,
            provisioning=cm,
            event_handlers={BillingEventType.customer_deleted: failing_callback},
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_delete_setup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_delete_subscription",
                event_type=BillingEventType.subscription_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID).plan_key == "pro"

        deleted = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_delete",
                event_type=BillingEventType.customer_deleted,
                occurred_at=_now(),
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        assert deleted.handled is True
        assert deleted.action == "customer_deleted"
        assert callback_events == [f"customer.deleted:{USER_ID}"]
        assert cm.get_user_plan(USER_ID).plan_id is None

        replay = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_delete",
                event_type=BillingEventType.customer_deleted,
                occurred_at=_now(),
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        assert replay.handled is True
        assert replay.action == "duplicate"

    def test_checkout_subscription_plan_change_and_cancellation_variants(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        intent = sink.create_or_get_checkout_intent(
            CheckoutIntentCreate(
                subject_id=USER_ID2,
                provider=PROVIDER,
                operation_key="checkout-subscription-lifecycle",
                checkout_kind="subscription",
                product_key="pro_monthly",
                request_digest="22" * 32,
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            )
        )
        subscription = BillingSubscriptionInfo(
            provider_subscription_id=SUB_ID2,
            status=BillingSubscriptionStatus.active,
            refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
            interval="month",
            interval_count=1,
        )
        checkout = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_checkout_subscription",
                event_type=BillingEventType.checkout_completed,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=subscription,
                metadata={"checkout_intent_id": intent.id},
            )
        )
        assert checkout.handled is True
        assert checkout.action == "subscription_created"
        completed_intent = sink.get_checkout_intent(intent.id, USER_ID2)
        assert completed_intent is not None
        assert completed_intent.status == "completed"
        assert cm.get_user_plan(USER_ID2).plan_key == "pro"

        changed = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_subscription_plan_changed",
                event_type=BillingEventType.subscription_plan_changed,
                occurred_at=_now(),
                account_id=USER_ID2,
                subscription=subscription.model_copy(
                    update={
                        "refs": ProviderRef(product_id=PRODUCT_ID, price_id="price_yearly_10000"),
                        "status": BillingSubscriptionStatus.active,
                    }
                ),
            )
        )
        assert changed.action == "plan_changed"
        assert cm.get_user_plan(USER_ID2).plan_key == "enterprise"

        scheduled = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_subscription_cancel_scheduled",
                event_type=BillingEventType.subscription_cancellation_scheduled,
                occurred_at=_now(),
                account_id=USER_ID2,
                subscription=subscription,
            )
        )
        assert scheduled.action == "cancellation_scheduled"
        scheduled_state = bs.get_billing_subscription(PROVIDER, SUB_ID2)
        assert scheduled_state is not None
        assert scheduled_state.cancel_at_period_end is True

        unscheduled = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_subscription_cancel_unscheduled",
                event_type=BillingEventType.subscription_cancellation_unscheduled,
                occurred_at=_now(),
                account_id=USER_ID2,
                subscription=subscription,
            )
        )
        assert unscheduled.action == "cancellation_unscheduled"
        unscheduled_state = bs.get_billing_subscription(PROVIDER, SUB_ID2)
        assert unscheduled_state is not None
        assert unscheduled_state.cancel_at_period_end is False

        canceled = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_subscription_canceled_variant",
                event_type=BillingEventType.subscription_canceled,
                occurred_at=_now(),
                account_id=USER_ID2,
                subscription=subscription.model_copy(update={"status": BillingSubscriptionStatus.canceled}),
            )
        )
        assert canceled.action == "subscription_canceled"
        canceled_state = bs.get_billing_subscription(PROVIDER, SUB_ID2)
        assert canceled_state is not None
        assert canceled_state.status == BillingSubscriptionStatus.canceled
        assert cm.get_user_plan(USER_ID2).plan_id is None

    def test_subscription_lifecycle_full(self, pg_database_url: str, pg_store: object) -> None:
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_1",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_create_1",
                event_type=BillingEventType.subscription_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID,
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                    interval="month",
                    interval_count=1,
                ),
            )
        )

        stored_sub = bs.get_billing_subscription(PROVIDER, SUB_ID)
        assert stored_sub is not None
        assert stored_sub.current_period_start is not None
        assert stored_sub.current_period_start.startswith("2025-06-01")
        assert stored_sub.current_period_end is not None
        assert stored_sub.current_period_end.startswith("2025-07-01")
        assert stored_sub.interval == "month"
        assert stored_sub.interval_count == 1

        plan = cm.get_user_plan(USER_ID)
        assert plan.plan_id is not None
        assert plan.plan_assigned_at is not None

        cancel_result = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cancel_1",
                event_type=BillingEventType.subscription_canceled,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID,
                    status=BillingSubscriptionStatus.canceled,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cancel_result.handled is True
        assert cancel_result.action == "subscription_canceled"

        plan2 = cm.get_user_plan(USER_ID)
        assert plan2.plan_id is None

    def test_topup_credit_grant(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_2",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_payment_2",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                payment=BillingPaymentInfo(
                    provider_payment_id="py_test456",
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )

        balance = cm.get_balance(USER_ID2)
        assert balance.balance == Decimal("2000")

    def test_refund_clawback_deducts_credits(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = "00000000-0000-0000-0000-000000000005"
        payment_id = "py_refund_clawback"

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_refund",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        balance_after_grant = cm.get_balance(uid)
        assert balance_after_grant.balance == Decimal("2000")

        result = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_1",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
                refund=BillingRefundInfo(
                    provider_refund_id="refund_1",
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    currency="USD",
                    status="succeeded",
                ),
            )
        )
        assert result.handled is True
        balance_after_refund = cm.get_balance(uid)
        assert balance_after_refund.balance == Decimal("0")

    def test_partial_refund_rounds_credit_clawback_to_six_places(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        precise_config = deepcopy(PRICING_DICT)
        precise_config["commerce"]["offers"]["standard_topup"]["credits_per_unit"] = "1234.567891"
        cm.publish_and_activate_catalog(precise_config)

        uid = USER_ID5
        payment_id = "py_refund_partial_precision"
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_partial_precision",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        assert cm.get_balance(uid).balance == Decimal("1234.567891")

        result = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_partial_precision",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                refund=BillingRefundInfo(
                    provider_refund_id="refund_partial_precision",
                    provider_payment_id=payment_id,
                    amount_minor=1,
                    currency="USD",
                    status="succeeded",
                ),
            )
        )
        assert result.handled is True
        assert cm.get_balance(uid).balance == Decimal("1233.333323")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT allocation.credit_amount, COUNT(ledger.id)
                FROM bursar.billing_refund_grants AS allocation
                JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
                JOIN bursar.credit_ledger_entries AS ledger ON ledger.id = allocation.ledger_entry_id
                WHERE refund.provider = %s AND refund.provider_refund_id = %s
                GROUP BY allocation.credit_amount
                """,
                (PROVIDER, "refund_partial_precision"),
            )
            assert cursor.fetchone() == (Decimal("1.234568"), 1)

    def test_duplicate_refund_identity_with_new_event_id_replays_one_clawback(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = USER_ID4
        payment_id = "py_refund_duplicate_identity"
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_duplicate_identity",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        refund = BillingRefundInfo(
            provider_refund_id="refund_duplicate_identity",
            provider_payment_id=payment_id,
            amount_minor=2000,
            currency="USD",
            status="succeeded",
        )
        first = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_duplicate_identity_1",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                refund=refund,
            )
        )
        second = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_duplicate_identity_2",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                refund=refund,
            )
        )
        assert first.handled is True
        assert second.handled is True
        assert second.action == "refund_clawback"
        assert cm.get_balance(uid).balance == Decimal("0")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM bursar.billing_refunds
                     WHERE provider = %s AND provider_refund_id = %s),
                    (SELECT COUNT(*) FROM bursar.billing_refund_grants AS allocation
                     JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
                     WHERE refund.provider = %s AND refund.provider_refund_id = %s),
                    (SELECT COUNT(*) FROM bursar.credit_ledger_entries AS ledger
                     JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
                     WHERE account.subject_id = %s::uuid AND ledger.kind = 'refund_clawback')
                """,
                (PROVIDER, "refund_duplicate_identity", PROVIDER, "refund_duplicate_identity", uid),
            )
            assert cursor.fetchone() == (1, 1, 1)

    @pytest.mark.concurrency
    def test_concurrent_distinct_events_same_refund_identity_post_once(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = USER_ID
        payment_id = "py_refund_concurrent_identity"
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_concurrent_identity",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        refund = BillingRefundInfo(
            provider_refund_id="refund_concurrent_identity",
            provider_payment_id=payment_id,
            amount_minor=2000,
            currency="USD",
            status="succeeded",
        )
        barrier = Barrier(8)

        def ingest(index: int):
            local_store = PostgresBillingStore(
                pg_database_url,
                tenant_id=TEST_TENANT_ID,
                provider_environment="test",
            )
            local_sink = BillingService(local_store)
            try:
                barrier.wait(timeout=30)
                return local_sink.ingest_billing_event(
                    BillingEvent(
                        provider=PROVIDER,
                        event_id=f"evt_refund_concurrent_identity_{index}",
                        event_type=BillingEventType.refund_created,
                        occurred_at=refund_occurred_at,
                        account_id=uid,
                        refund=refund,
                    )
                )
            finally:
                local_store.close()

        refund_occurred_at = _now()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(ingest, range(8)))

        assert all(result.handled is True for result in results)
        assert all(result.action == "refund_clawback" for result in results)
        assert cm.get_balance(uid).balance == Decimal("0")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM bursar.billing_refunds
                     WHERE provider = %s AND provider_refund_id = %s),
                    (SELECT COUNT(*) FROM bursar.billing_refund_grants AS allocation
                     JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
                     WHERE refund.provider = %s AND refund.provider_refund_id = %s),
                    (SELECT COUNT(*) FROM bursar.credit_ledger_entries AS ledger
                     JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
                     WHERE account.subject_id = %s::uuid AND ledger.kind = 'refund_clawback')
                """,
                (PROVIDER, refund.provider_refund_id, PROVIDER, refund.provider_refund_id, uid),
            )
            assert cursor.fetchone() == (1, 1, 1)

    def test_cumulative_partial_refunds_round_to_six_places_and_fully_claw_back(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        precise_config = deepcopy(PRICING_DICT)
        precise_config["commerce"]["offers"]["standard_topup"]["credits_per_unit"] = "1234.567891"
        cm.publish_and_activate_catalog(precise_config)

        uid = USER_ID2
        payment_id = "py_refund_cumulative_precision"
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_cumulative_precision",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=1000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        expected_credit_amounts = [
            Decimal("151.851851"),
            Decimal("562.962958"),
            Decimal("519.753082"),
        ]
        expected_balances = [
            Decimal("1082.716040"),
            Decimal("519.753082"),
            Decimal("0.000000"),
        ]
        observed_refund_ids: list[str] = []
        for index, (amount_minor, expected_credit, expected_balance) in enumerate(
            zip((123, 456, 421), expected_credit_amounts, expected_balances, strict=True),
            start=1,
        ):
            provider_refund_id = f"refund_cumulative_precision_{index}"
            observed_refund_ids.append(provider_refund_id)
            result = sink.ingest_billing_event(
                BillingEvent(
                    provider=PROVIDER,
                    event_id=f"evt_refund_cumulative_precision_{index}",
                    event_type=BillingEventType.refund_created,
                    occurred_at=_now(),
                    account_id=uid,
                    refund=BillingRefundInfo(
                        provider_refund_id=provider_refund_id,
                        provider_payment_id=payment_id,
                        amount_minor=amount_minor,
                        currency="USD",
                        status="succeeded",
                    ),
                )
            )
            assert result.handled is True
            assert result.action == "refund_clawback"
            assert cm.get_balance(uid).balance == expected_balance

            with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
                _bind_tenant(cursor)
                cursor.execute(
                    """
                    SELECT allocation.credit_amount
                    FROM bursar.billing_refund_grants AS allocation
                    JOIN bursar.billing_refunds AS refund ON refund.id = allocation.refund_id
                    WHERE refund.provider = %s AND refund.provider_refund_id = %s
                    """,
                    (PROVIDER, provider_refund_id),
                )
                assert cursor.fetchone() == (expected_credit,)

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM bursar.credit_ledger_entries AS ledger
                JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
                WHERE account.subject_id = %s::uuid AND ledger.kind = 'refund_clawback'
                """,
                (uid,),
            )
            assert cursor.fetchone() == (3,)

    def test_pending_refund_waits_for_succeeded_transition_and_replay_is_single_clawback(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = USER_ID3
        payment_id = "py_refund_pending_then_succeeded"
        payment_occurred_at = datetime.now(UTC)
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_pending_then_succeeded",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=payment_occurred_at.isoformat(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        refund_id = "refund_pending_then_succeeded"
        pending_at = payment_occurred_at + timedelta(seconds=1)
        succeeded_at = payment_occurred_at + timedelta(seconds=2)
        pending = BillingEvent(
            provider=PROVIDER,
            event_id="evt_refund_pending_then_succeeded_pending",
            event_type=BillingEventType.refund_created,
            occurred_at=pending_at.isoformat(),
            account_id=uid,
            refund=BillingRefundInfo(
                provider_refund_id=refund_id,
                provider_payment_id=payment_id,
                amount_minor=1000,
                currency="USD",
                status="pending",
            ),
        )
        pending_result = sink.ingest_billing_event(pending)
        assert pending_result.handled is True
        assert pending_result.action == "refund_recorded"
        assert cm.get_balance(uid).balance == Decimal("2000")

        pending_refund = pending.refund
        assert pending_refund is not None
        succeeded_refund = pending_refund.model_copy(update={"status": "succeeded"})
        succeeded = pending.model_copy(
            update={
                "event_id": "evt_refund_pending_then_succeeded_succeeded",
                "event_type": BillingEventType.refund_updated,
                "occurred_at": succeeded_at.isoformat(),
                "refund": succeeded_refund,
            }
        )
        succeeded_result = sink.ingest_billing_event(succeeded)
        assert succeeded_result.handled is True
        assert succeeded_result.action == "refund_clawback"
        assert cm.get_balance(uid).balance == Decimal("1000")

        replay = succeeded.model_copy(
            update={
                "event_id": "evt_refund_pending_then_succeeded_replay",
                "occurred_at": (payment_occurred_at + timedelta(seconds=3)).isoformat(),
            }
        )
        replay_result = sink.ingest_billing_event(replay)
        assert replay_result.handled is True
        assert replay_result.action == "refund_clawback"
        assert cm.get_balance(uid).balance == Decimal("1000")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT refund.status, COUNT(DISTINCT allocation.refund_id), COUNT(DISTINCT ledger.id)
                FROM bursar.billing_refunds AS refund
                LEFT JOIN bursar.billing_refund_grants AS allocation ON allocation.refund_id = refund.id
                LEFT JOIN bursar.credit_ledger_entries AS ledger ON ledger.id = allocation.ledger_entry_id
                WHERE refund.provider = %s AND refund.provider_refund_id = %s
                GROUP BY refund.status
                """,
                (PROVIDER, refund_id),
            )
            assert cursor.fetchone() == ("succeeded", 1, 1)

    def test_over_refund_event_is_rejected_without_a_second_credit_mutation(
        self, pg_database_url: str, pg_store: object
    ) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = USER_ID3
        payment_id = "py_refund_overallocation"
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund_overallocation",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=uid,
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )
        first = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_overallocation_1",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                refund=BillingRefundInfo(
                    provider_refund_id="refund_overallocation_1",
                    provider_payment_id=payment_id,
                    amount_minor=1500,
                    currency="USD",
                    status="succeeded",
                ),
            )
        )
        second = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_overallocation_2",
                event_type=BillingEventType.refund_created,
                occurred_at=_now(),
                account_id=uid,
                refund=BillingRefundInfo(
                    provider_refund_id="refund_overallocation_2",
                    provider_payment_id=payment_id,
                    amount_minor=600,
                    currency="USD",
                    status="succeeded",
                ),
            )
        )
        assert first.handled is True
        assert second.handled is False
        assert second.error is not None
        assert cm.get_balance(uid).balance == Decimal("500")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM bursar.billing_refunds
                WHERE provider = %s AND payment_id = (
                    SELECT id FROM bursar.billing_payments
                    WHERE provider = %s AND provider_payment_id = %s
                ) AND status = 'succeeded'
                """,
                (PROVIDER, PROVIDER, payment_id),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM bursar.credit_ledger_entries AS ledger
                JOIN bursar.credit_accounts AS account ON account.id = ledger.account_id
                WHERE account.subject_id = %s::uuid AND ledger.kind = 'refund_clawback'
                """,
                (uid,),
            )
            assert cursor.fetchone() == (1,)

    def test_subscription_pause_resume(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_pause",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_1",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is not None

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_2",
                event_type=BillingEventType.subscription_paused,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is None

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_3",
                event_type=BillingEventType.subscription_resumed,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is not None

    def test_unknown_event_type_is_rejected_at_the_public_contract(self) -> None:
        with pytest.raises(ValueError, match="event_type"):
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_unknown",
                event_type="some.unknown.event",  # type: ignore[arg-type]
                occurred_at=_now(),
                account_id=USER_ID,
            )

    def test_duplicate_event_skips_side_effects(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_dup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        assert bs.get_billing_customer(PROVIDER, "cus_dup_test") == USER_ID

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_dup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        assert bs.get_billing_customer(PROVIDER, "cus_dup_test") == USER_ID

    def test_provider_scoped_event_id(self, pg_database_url: str, pg_store: object) -> None:
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)

        c1 = bs.claim_billing_event("stripe", "evt_prov_scope", "test.event")
        assert c1.status == "claimed"

        c2 = bs.claim_billing_event("dodo", "evt_prov_scope", "test.event")
        assert c2.status == "claimed"

    def test_sync_offers_adds_new(self, pg_database_url: str, pg_store: object) -> None:
        bs, cm, _sink = _make_components(pg_database_url, pg_store)
        config = deepcopy(PRICING_DICT)
        config["commerce"]["offers"]["new_offer"] = {
            "type": "subscription",
            "display_name": "New Offer",
            "price": {"amount_minor": 1000, "currency": "USD"},
            "plan": "free",
            "billing_interval": {"unit": "month", "count": 1},
            "providers": {
                "stripe": {
                    "type": "stripe_price",
                    "price_id": "price_new_offer",
                },
            },
        }
        cm.publish_and_activate_catalog(config)
        new_offer = bs.resolve_billing_offer("stripe", product_id=None, price_id="price_new_offer")
        assert new_offer is not None
        assert new_offer.offer_key == "new_offer"

    def test_cycle_grant_credits_granted(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg1",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID3,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg1",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                account_id=USER_ID3,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_test",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(
                        product_id="prod_cycle_grant",
                        price_id="price_cycle_grant_5000",
                    ),
                    interval="month",
                    interval_count=1,
                ),
            )
        )
        balance = cm.get_balance(USER_ID3)
        assert balance.balance == Decimal("5000")

    def test_cycle_grant_replace_prior(self, pg_database_url: str, pg_store: object) -> None:
        _bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg2",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg2a",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                account_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(
                        product_id="prod_cycle_grant",
                        price_id="price_cycle_grant_5000",
                    ),
                    interval="month",
                    interval_count=1,
                ),
            )
        )
        balance1 = cm.get_balance(USER_ID4)
        assert balance1.balance == Decimal("5000")

        # Renew — should revoke prior cycle_grant and grant new 5000
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg2b",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                account_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-07-01T00:00:00Z",
                    period_end="2025-08-01T00:00:00Z",
                    refs=ProviderRef(
                        product_id="prod_cycle_grant",
                        price_id="price_cycle_grant_5000",
                    ),
                    interval="month",
                    interval_count=1,
                ),
            )
        )
        balance2 = cm.get_balance(USER_ID4)
        assert balance2.balance == Decimal("5000")


class TestDodoBillingIntegration:
    def test_full_subscription_lifecycle(self, pg_database_url: str, pg_store: object) -> None:
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        # customer created — ingest directly
        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_lifecycle",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_lifecycle"),
            )
        )

        # subscription.active → subscription.created via Dodo mapper
        asyncio.run(
            map_dodo_event(
                "subscription.active",
                {
                    "subscription_id": "sub_dodo_lifecycle",
                    "status": "active",
                    "product_id": DODO_PRODUCT_ID,
                    "payment_frequency_interval": "Month",
                    "payment_frequency_count": 1,
                    "previous_billing_date": datetime.now(UTC).strftime(
                        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
                    ),
                    "next_billing_date": (datetime.now(UTC) + timedelta(days=30)).strftime(
                        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
                    ),
                },
                USER_ID5,
                {},
                sink,
            )
        )

        stored = bs.get_billing_subscription("dodo", "sub_dodo_lifecycle")
        assert stored is not None
        assert stored.status == "active"
        assert stored.interval == "month"
        assert stored.interval_count == 1
        assert stored.current_period_start is not None
        assert stored.current_period_start.startswith("202")
        assert stored.current_period_end is not None
        assert stored.current_period_end.startswith("202")

        plan = cm.get_user_plan(USER_ID5)
        assert plan.plan_id is not None
        assert plan.plan_assigned_at is not None

    def test_duplicate_event_returns_duplicate(self, pg_database_url: str, pg_store: object) -> None:
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_dup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_dup"),
            )
        )

        asyncio.run(
            map_dodo_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_dup", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        subscription = bs.get_billing_subscription("dodo", "sub_dodo_dup")
        assert subscription is not None
        assert subscription.status == "active"

        asyncio.run(
            map_dodo_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_dup", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        subscription = bs.get_billing_subscription("dodo", "sub_dodo_dup")
        assert subscription is not None
        assert subscription.status == "active"

    def test_multiple_events_distinct_ids(self, pg_database_url: str, pg_store: object) -> None:
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_multi",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_multi"),
            )
        )

        asyncio.run(
            map_dodo_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_multi_1", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        asyncio.run(
            map_dodo_event(
                "subscription.renewed",
                {"subscription_id": "sub_dodo_multi_1", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        asyncio.run(
            map_dodo_event(
                "subscription.updated",
                {"subscription_id": "sub_dodo_multi_1", "status": "active"},
                USER_ID5,
                {},
                sink,
            )
        )

        assert bs.get_billing_subscription("dodo", "sub_dodo_multi_1") is not None

    def test_js_date_parsed_to_valid_iso(self, pg_database_url: str, pg_store: object) -> None:
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_date",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_date"),
            )
        )

        js_date = datetime.now(UTC).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        js_date_future = (datetime.now(UTC) + timedelta(days=30)).strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
        )

        asyncio.run(
            map_dodo_event(
                "subscription.active",
                {
                    "subscription_id": "sub_dodo_date",
                    "status": "active",
                    "product_id": DODO_PRODUCT_ID,
                    "previous_billing_date": js_date,
                    "next_billing_date": js_date_future,
                },
                USER_ID5,
                {},
                sink,
            )
        )

        sub = bs.get_billing_subscription("dodo", "sub_dodo_date")
        assert sub is not None
        assert sub.current_period_start is not None
        assert sub.current_period_start.startswith("202")
        assert sub.current_period_end is not None
        assert sub.current_period_end.startswith("202")
