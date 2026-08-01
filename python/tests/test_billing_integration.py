"""Integration tests for PostgresBillingStore + BillingService — mirrors
JavaScript tests/billing-integration.test.ts.

Tests sync/resolve round-trips, customer/subscription CRUD, event
idempotency, topup credits, and the full subscription lifecycle against
a real Postgres 16.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import psycopg2
import psycopg2.pool
import pytest

from bursar.billing.billing_service import BillingService
from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.types import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingPaymentInfo,
    BillingRefundInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.credits.service import CreditsService
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
from tests.conftest import TEST_TENANT_ID

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
        "accounting": {
            "unit": "credit",
            "scale": 6,
            "rounding": "half_up",
        },
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


def _bootstrap_auth_users(pg_database_url: str) -> None:
    conn = psycopg2.connect(pg_database_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for uid in (USER_ID, USER_ID2, USER_ID3, USER_ID4, USER_ID5):
                cur.execute(
                    "INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (uid,),
                )
    finally:
        conn.close()


def _bind_tenant(cursor: psycopg2.extensions.cursor) -> None:
    cursor.execute(
        "SELECT set_config('bursar.tenant_id', %s, true)",
        (TEST_TENANT_ID,),
    )


def _make_components(
    pg_database_url: str,
    pg_store: object,
) -> tuple[PostgresBillingStore, CreditsService, BillingService]:
    bs = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
    )
    cm = CreditsService(pg_store)  # type: ignore[arg-type]
    cm.publish_pricing_from_dict(PRICING_DICT)
    sink = BillingService(bs, provisioning=cm)
    return bs, cm, sink


# ── Sync + Resolve ─────────────────────────────────────────────────────


class TestBillingSync:
    def test_config_sync_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        offer = bs.resolve_billing_offer(PROVIDER, product_id=None, price_id=PRICE_ID)
        assert offer is not None
        assert offer.offer_key == "pro_monthly"
        assert offer.plan == "pro"

    def test_config_resolve_by_canonical_lookup(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        offer = bs.resolve_billing_offer(PROVIDER, price_id=PRICE_ID)
        assert offer is not None
        assert offer.offer_key == "pro_monthly"

    def test_topup_config_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, product_id=None, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert topup.topup_key == "standard_topup"
        assert topup.credits_per_unit == 1000

    def test_unresolved_offer_returns_null(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.resolve_billing_offer(PROVIDER, product_id=None, price_id="nonexistent") is None

    def test_resolve_billing_offer_no_match(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.resolve_billing_offer("nonexistent_provider", product_id=None, price_id=PRICE_ID) is None


# ── Checkout intent idempotency ───────────────────────────────────────


class TestCheckoutIntentIdempotency:
    def test_terminal_checkout_replay_does_not_reopen_provider_attempt(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bootstrap_auth_users(pg_database_url)
        _make_components(pg_database_url, pg_store)
        first_expiry = datetime.now(UTC) + timedelta(hours=1)
        retry_expiry = first_expiry + timedelta(hours=1)
        digest = "11" * 32

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, decode(%s, 'hex'),
                    %s::timestamptz, %s, %s
                )
                """,
                (
                    USER_ID,
                    PROVIDER,
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
                    %s::uuid, %s, %s, %s, decode(%s, 'hex'),
                    %s::timestamptz, %s, %s
                )
                """,
                (
                    USER_ID,
                    PROVIDER,
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

    def test_checkout_replay_reopens_stale_intent_without_reusing_provider_session(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bootstrap_auth_users(pg_database_url)
        _make_components(pg_database_url, pg_store)
        digest = "22" * 32
        retry_expiry = datetime.now(UTC) + timedelta(hours=2)

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            _bind_tenant(cursor)
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz,
                    %s, %s
                )
                """,
                (
                    USER_ID2,
                    PROVIDER,
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
                """,
                (intent_id,),
            )
            cursor.execute(
                """
                SELECT bursar.create_checkout_intent(
                    %s::uuid, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz
                )
                """,
                (
                    USER_ID2,
                    PROVIDER,
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
            assert cursor.fetchone() == ("open", None, None, retry_expiry)


# ── Auto-recharge profile ─────────────────────────────────────────────


class TestAutoRechargeProfile:
    def test_eligible_projected_topup_can_enable_auto_recharge(
        self,
        pg_database_url: str,
        pg_store: object,
    ) -> None:
        _bootstrap_auth_users(pg_database_url)
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
        CreditsService(pg_store).publish_pricing_from_dict(config)  # type: ignore[arg-type]

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
        _bootstrap_auth_users(pg_database_url)
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
        _bootstrap_auth_users(pg_database_url)
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
                    status
                )
                VALUES
                    (
                        %s::uuid, %s, 'live', 'sub-live',
                        %s::uuid, %s::uuid, 'active'
                    ),
                    (
                        %s::uuid, %s, 'test', 'sub-test',
                        %s::uuid, %s::uuid, 'active'
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
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID, "test@example.com")
        uid = bs.get_billing_customer(PROVIDER, CUSTOMER_ID)
        assert uid == USER_ID

    def test_customer_not_found(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.get_billing_customer(PROVIDER, "nonexistent_cus") is None

    def test_customer_remap_to_different_user_rejected(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID2)
        assert bs.get_billing_customer(PROVIDER, CUSTOMER_ID) == USER_ID

    def test_multiple_providers_same_customer_id(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_customer("stripe", CUSTOMER_ID, USER_ID)
        bs.upsert_billing_customer("dodo", CUSTOMER_ID, USER_ID2)
        assert bs.get_billing_customer("stripe", CUSTOMER_ID) == USER_ID
        assert bs.get_billing_customer("dodo", CUSTOMER_ID) == USER_ID2


# ── Subscription CRUD ─────────────────────────────────────────────────


class TestSubscriptionCrud:
    def test_subscription_upsert_and_read(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
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
        )
        bs.upsert_billing_subscription(state)
        result = bs.get_billing_subscription(PROVIDER, SUB_ID)
        assert result is not None
        assert result.user_id == USER_ID
        assert result.status == BillingSubscriptionStatus.active
        assert result.plan == "pro"

    def test_subscription_not_found(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        assert bs.get_billing_subscription(PROVIDER, "nonexistent_sub") is None

    def test_subscription_update(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                offer_key="pro_monthly",
                status=BillingSubscriptionStatus.active,
            )
        )
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                status=BillingSubscriptionStatus.canceled,
            )
        )
        sub = bs.get_billing_subscription(PROVIDER, SUB_ID)
        assert sub is not None
        assert sub.status == BillingSubscriptionStatus.canceled


# ── Event idempotency ──────────────────────────────────────────────────


class TestEventIdempotency:
    def test_event_idempotency(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, sink = _make_components(pg_database_url, pg_store)
        event = BillingEvent(
            provider=PROVIDER,
            event_id=EVENT_ID,
            event_type=BillingEventType.customer_created,
            occurred_at=_now(),
            user_id=USER_ID,
        )
        r1 = sink.ingest_billing_event(event)
        assert r1.handled is True
        r2 = sink.ingest_billing_event(event)
        assert r2.action == "duplicate"

    def test_event_claim_complete_fail_cycle(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        c1 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c1.status == "claimed"
        assert c1.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_claim_cycle", c1.claim_token)
        c2 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c2.status == "duplicate"

    def test_event_fail_then_reclaim(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        c1 = bs.claim_billing_event(PROVIDER, "evt_fail_retry", "test.event")
        assert c1.status == "claimed"
        assert c1.claim_token is not None
        bs.fail_billing_event(PROVIDER, "evt_fail_retry", c1.claim_token, "retryable test failure")
        c2 = bs.claim_billing_event(PROVIDER, "evt_fail_retry", "test.event")
        assert c2.status == "claimed"

    @pytest.mark.concurrency
    def test_concurrent_event_claims_admit_one_worker(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        barrier = Barrier(12)

        def claim(_: int):
            pool = psycopg2.pool.ThreadedConnectionPool(1, 1, pg_database_url)
            local = PostgresBillingStore(
                pg_database_url,
                tenant_id=TEST_TENANT_ID,
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
        assert sum(c.status in ("duplicate", "busy", "retry") for c in claims) == 11
        winner = next(c for c in claims if c.status == "claimed")
        assert winner.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_concurrent_claim", winner.claim_token)
        assert bs.claim_billing_event(PROVIDER, "evt_concurrent_claim", "test.event").status == "duplicate"

    def test_event_handler_dispatched_for_matching_event(self, pg_database_url: str, pg_store: object) -> None:
        """Mirrors JavaScript test: eventHandlers dispatch on matching event type."""
        _bootstrap_auth_users(pg_database_url)
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
                user_id=USER_ID,
            ),
        )
        assert result.handled is True
        assert called is True


# ── Topup credits ──────────────────────────────────────────────────────


class TestTopup:
    def test_compute_topup_credits(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert bs.compute_topup_credits(2000, topup) == 2000

    def test_compute_topup_credits_odd_amount(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        topup = bs.resolve_credit_topup(PROVIDER, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert bs.compute_topup_credits(1999, topup) == 0


# ── BillingService lifecycle ───────────────────────────────────────────────


class TestBillingServiceLifecycle:
    def test_subscription_lifecycle_full(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_1",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_create_1",
                event_type=BillingEventType.subscription_created,
                occurred_at=_now(),
                user_id=USER_ID,
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
                user_id=USER_ID,
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
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_2",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_payment_2",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                payment=BillingPaymentInfo(
                    provider_payment_id="py_test456",
                    amount_minor=2000,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
                ),
            )
        )

        balance = cm.get_balance(USER_ID2)
        assert balance.balance == Decimal("2000")

    def test_refund_clawback_deducts_credits(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        uid = "00000000-0000-0000-0000-000000000005"
        payment_id = "py_refund_clawback"

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_refund",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                user_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
                payment=BillingPaymentInfo(
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=PRICE_ID_TOPUP),
                    purpose="credit_topup",
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
                user_id=uid,
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

    def test_subscription_pause_resume(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_pause",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_1",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                user_id=USER_ID2,
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
                user_id=USER_ID2,
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
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is not None

    def test_unknown_event_type_ignored(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, sink = _make_components(pg_database_url, pg_store)
        result = sink.ingest_billing_event(
            BillingEvent.model_construct(
                provider=PROVIDER,
                event_id="evt_unknown",
                event_type="some.unknown.event",
                occurred_at=_now(),
                user_id=USER_ID,
            )
        )
        assert result.handled is False
        assert result.error == "unhandled_event_type"

    def test_duplicate_event_skips_side_effects(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_dup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID,
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
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        assert bs.get_billing_customer(PROVIDER, "cus_dup_test") == USER_ID

    def test_provider_scoped_event_id(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)

        c1 = bs.claim_billing_event("stripe", "evt_prov_scope", "test.event")
        assert c1.status == "claimed"

        c2 = bs.claim_billing_event("dodo", "evt_prov_scope", "test.event")
        assert c2.status == "claimed"

    def test_sync_offers_adds_new(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
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
        cm.publish_pricing_from_dict(config)
        new_offer = bs.resolve_billing_offer("stripe", product_id=None, price_id="price_new_offer")
        assert new_offer is not None
        assert new_offer.offer_key == "new_offer"

    def test_cycle_grant_credits_granted(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg1",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID3,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg1",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                user_id=USER_ID3,
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
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg2",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg2a",
                event_type=BillingEventType.subscription_renewed,
                occurred_at=_now(),
                user_id=USER_ID4,
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
                user_id=USER_ID4,
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
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)

        # customer created — ingest directly
        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_lifecycle",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_lifecycle"),
            )
        )

        # subscription.active → subscription.created via Dodo mapper
        asyncio.run(
            handle_dodo_billing_event(
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
        _bootstrap_auth_users(pg_database_url)
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_dup",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_dup"),
            )
        )

        asyncio.run(
            handle_dodo_billing_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_dup", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        assert bs.get_billing_subscription("dodo", "sub_dodo_dup").status == "active"

        asyncio.run(
            handle_dodo_billing_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_dup", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        assert bs.get_billing_subscription("dodo", "sub_dodo_dup").status == "active"

    def test_multiple_events_distinct_ids(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_multi",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_multi"),
            )
        )

        asyncio.run(
            handle_dodo_billing_event(
                "subscription.active",
                {"subscription_id": "sub_dodo_multi_1", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        asyncio.run(
            handle_dodo_billing_event(
                "subscription.renewed",
                {"subscription_id": "sub_dodo_multi_1", "status": "active", "product_id": DODO_PRODUCT_ID},
                USER_ID5,
                {},
                sink,
            )
        )
        asyncio.run(
            handle_dodo_billing_event(
                "subscription.updated",
                {"subscription_id": "sub_dodo_multi_1", "status": "active"},
                USER_ID5,
                {},
                sink,
            )
        )

        assert bs.get_billing_subscription("dodo", "sub_dodo_multi_1") is not None

    def test_js_date_parsed_to_valid_iso(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _, sink = _make_components(pg_database_url, pg_store)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_date",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                user_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_date"),
            )
        )

        js_date = datetime.now(UTC).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        js_date_future = (datetime.now(UTC) + timedelta(days=30)).strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
        )

        asyncio.run(
            handle_dodo_billing_event(
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
