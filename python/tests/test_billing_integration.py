"""Integration tests for PostgresBillingStore + BillingServiceImpl — mirrors
JavaScript tests/billing-integration.test.ts.

Tests sync/resolve round-trips, customer/subscription CRUD, event
idempotency, topup credits, and the full subscription lifecycle against
a real Postgres 16.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier

import psycopg2
import psycopg2.pool
import pytest

from bursar.billing.billing_service import BillingServiceImpl
from bursar.billing.models import (
    AllowanceGrant,
    BillingConfig,
    BillingCreditTopup,
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingOffer,
    BillingPaymentInfo,
    BillingRefundInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CycleGrant,
    ProviderRef,
)
from bursar.billing.postgres import PostgresBillingStore
from bursar.credits_service import CreditsService
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event

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
    "metering": {
        "models": {"*": "input_tokens * 1"},
    },
    "ledger": {
        "min_balance": 0,
        "buckets": {
            "purchased": {
                "label": "Purchased",
                "priority": 1,
                "default": True,
                "allow_overdraft": False,
            },
        },
    },
    "plans": {
        "free": {"label": "Free", "allowance": {"amount": 1000}},
        "pro": {"label": "Pro", "allowance": {"amount": 100000}},
        "enterprise": {"label": "Enterprise", "allowance": {"amount": 1000000}},
    },
}

BILLING_CONFIG = BillingConfig(
    subscriptions={
        "pro_monthly": BillingOffer(
            plan="pro",
            interval="month",
            interval_count=1,
            grant=AllowanceGrant(),
            valid_from="2025-01-01",
            valid_to="2026-12-31",
            providers={
                "stripe": ProviderRef(product_id="prod_monthly", price_id="price_monthly_1000"),
                "dodo": ProviderRef(product_id="prod_dodo_monthly", price_id="price_dodo_monthly_1000"),
            },
        ),
        "enterprise_yearly": BillingOffer(
            plan="enterprise",
            interval="year",
            interval_count=1,
            grant=AllowanceGrant(),
            valid_from="2025-06-01",
            providers={
                "stripe": ProviderRef(product_id="prod_yearly", price_id="price_yearly_10000"),
            },
        ),
        "cycle_grant_monthly": BillingOffer(
            plan="pro",
            interval="month",
            interval_count=1,
            grant=CycleGrant(credits=5000, bucket="purchased", replace_prior=True),
            valid_to=None,
            providers={
                "stripe": ProviderRef(
                    product_id="prod_cycle_grant",
                    price_id="price_cycle_grant_5000",
                ),
            },
        ),
    },
    topups={
        "standard_topup": BillingCreditTopup(
            credits_per_unit=1000,
            deposit_to="purchased",
            min_amount_minor=500,
            max_amount_minor=50000,
            providers={
                "stripe": ProviderRef(product_id="prod_topup", price_id="price_topup_credits"),
            },
        ),
    },
)


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


def _make_components(
    pg_database_url: str,
    pg_store: object,
) -> tuple[PostgresBillingStore, CreditsService, BillingServiceImpl]:
    bs = PostgresBillingStore(pg_database_url)
    cm = CreditsService(pg_store)  # type: ignore[arg-type]
    cm.publish_pricing_from_dict(PRICING_DICT)
    sink = BillingServiceImpl(bs, provisioning=cm)
    return bs, cm, sink


# ── Sync + Resolve ─────────────────────────────────────────────────────


class TestBillingSync:
    def test_config_sync_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        offer = bs.resolve_billing_offer(PROVIDER, product_id=None, price_id=PRICE_ID)
        assert offer is not None
        assert offer.offer_key == "pro_monthly"
        assert offer.plan == "pro"

    def test_config_resolve_by_product_id(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        offer = bs.resolve_billing_offer(PROVIDER, product_id="prod_monthly")
        assert offer is not None
        assert offer.offer_key == "pro_monthly"

    def test_topup_config_roundtrip(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        topup = bs.resolve_credit_topup(PROVIDER, product_id=None, price_id=PRICE_ID_TOPUP)
        assert topup is not None
        assert topup.topup_key == "standard_topup"
        assert topup.credits_per_unit == 1000

    def test_unresolved_offer_returns_null(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        assert bs.resolve_billing_offer(PROVIDER, product_id=None, price_id="nonexistent") is None

    def test_resolve_billing_offer_no_match(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        assert bs.resolve_billing_offer("nonexistent_provider", product_id=None, price_id=PRICE_ID) is None


# ── Customer CRUD ──────────────────────────────────────────────────────


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
        result = bs.upsert_billing_customer(PROVIDER, CUSTOMER_ID, USER_ID2)
        assert result.get("error") == "user_id_mismatch"
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
        bs.sync_billing_from_config(BILLING_CONFIG)
        state = BillingSubscriptionState(
            user_id=USER_ID,
            provider=PROVIDER,
            provider_subscription_id=SUB_ID,
            provider_customer_id=CUSTOMER_ID,
            offer_key="pro_monthly",
            plan="pro",
            status="active",
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
        bs.sync_billing_from_config(BILLING_CONFIG)
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                status="active",
            )
        )
        bs.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=USER_ID,
                provider=PROVIDER,
                provider_subscription_id=SUB_ID,
                status="canceled",
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
        bs.sync_billing_from_config(BILLING_CONFIG)
        event = BillingEvent(
            provider=PROVIDER,
            event_id=EVENT_ID,
            event_type="customer.created",
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
        bs.sync_billing_from_config(BILLING_CONFIG)
        c1 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c1.status == "claimed"
        assert c1.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_claim_cycle", c1.claim_token)
        c2 = bs.claim_billing_event(PROVIDER, "evt_claim_cycle", "test.event")
        assert c2.status == "duplicate"

    def test_event_fail_then_reclaim(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
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
        bs.sync_billing_from_config(BILLING_CONFIG)
        barrier = Barrier(12)

        def claim(_: int):
            pool = psycopg2.pool.ThreadedConnectionPool(1, 1, pg_database_url)
            local = PostgresBillingStore(pg_database_url, pool=pool)
            try:
                barrier.wait(timeout=30)
                return local.claim_billing_event(PROVIDER, "evt_concurrent_claim", "test.event")
            finally:
                local.close()

        with ThreadPoolExecutor(max_workers=12) as executor:
            claims = list(executor.map(claim, range(12)))
        assert sum(c.status == "claimed" for c in claims) == 1
        assert sum(c.status in ("duplicate", "retry") for c in claims) == 11
        winner = next(c for c in claims if c.status == "claimed")
        assert winner.claim_token is not None
        bs.complete_billing_event(PROVIDER, "evt_concurrent_claim", winner.claim_token)
        assert bs.claim_billing_event(PROVIDER, "evt_concurrent_claim", "test.event").status == "duplicate"

    def test_event_handler_dispatched_for_matching_event(self, pg_database_url: str, pg_store: object) -> None:
        """Mirrors JavaScript test: eventHandlers dispatch on matching event type."""
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        called = False

        def handler(event: BillingEvent, user_id: str) -> None:
            nonlocal called
            called = True

        sink = BillingServiceImpl(
            bs,
            provisioning=_cm,
            event_handlers={
                BillingEventType.subscription_trial_will_end: handler,
            },
        )
        bs.sync_billing_from_config(BILLING_CONFIG)
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
        _bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        result = BillingServiceImpl._compute_topup_credits(2000, 1000)
        assert result == 20000

    def test_compute_topup_credits_odd_amount(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        _bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        result = BillingServiceImpl._compute_topup_credits(1999, 1000)
        assert result == 19990


# ── BillingServiceImpl lifecycle ───────────────────────────────────────────


class TestBillingServiceImplLifecycle:
    def test_subscription_lifecycle_full(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_1",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_create_1",
                event_type="subscription.created",
                occurred_at=_now(),
                user_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID,
                    status="active",
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
                event_type="subscription.canceled",
                occurred_at=_now(),
                user_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID,
                    status="canceled",
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_customer_2",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_payment_2",
                event_type="payment.succeeded",
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
        assert balance.balance == Decimal("20000")

    def test_refund_clawback_deducts_credits(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
        uid = "00000000-0000-0000-0000-000000000005"
        payment_id = "py_refund_clawback"

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_refund",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_pay_refund",
                event_type="payment.succeeded",
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
        assert balance_after_grant.balance == Decimal("20000")

        result = sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_refund_1",
                event_type="refund.created",
                occurred_at=_now(),
                user_id=uid,
                customer=BillingCustomerInfo(provider_customer_id="cus_refund_test"),
                refund=BillingRefundInfo(
                    provider_refund_id="refund_1",
                    provider_payment_id=payment_id,
                    amount_minor=2000,
                    currency="USD",
                ),
            )
        )
        assert result.handled is True
        balance_after_refund = cm.get_balance(uid)
        assert balance_after_refund.balance == Decimal("0")

    def test_subscription_pause_resume(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_pause",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_1",
                event_type="subscription.created",
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                    status="active",
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is not None

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_pause_2",
                event_type="subscription.paused",
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
                event_type="subscription.resumed",
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=SUB_ID2,
                    status="active",
                    refs=ProviderRef(product_id=PRODUCT_ID, price_id=PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(USER_ID2).plan_id is not None

    def test_unknown_event_type_ignored(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_dup",
                event_type="customer.created",
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
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        assert bs.get_billing_customer(PROVIDER, "cus_dup_test") == USER_ID

    def test_provider_scoped_event_id(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        c1 = bs.claim_billing_event("stripe", "evt_prov_scope", "test.event")
        assert c1.status == "claimed"

        c2 = bs.claim_billing_event("dodo", "evt_prov_scope", "test.event")
        assert c2.status == "claimed"

    def test_sync_offers_adds_new(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, _cm, _sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        bs.sync_billing_from_config(
            BillingConfig(
                subscriptions={
                    "new_offer": BillingOffer(
                        plan="free",
                        interval="month",
                        providers={
                            "stripe": ProviderRef(price_id="price_new_offer"),
                        },
                    ),
                },
            )
        )
        new_offer = bs.resolve_billing_offer("stripe", product_id=None, price_id="price_new_offer")
        assert new_offer is not None
        assert new_offer.offer_key == "new_offer"

    def test_cycle_grant_credits_granted(self, pg_database_url: str, pg_store: object) -> None:
        _bootstrap_auth_users(pg_database_url)
        bs, cm, sink = _make_components(pg_database_url, pg_store)
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg1",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID3,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg1",
                event_type="subscription.created",
                occurred_at=_now(),
                user_id=USER_ID3,
                customer=BillingCustomerInfo(provider_customer_id=CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_test",
                    status="active",
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_cus_cg2",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
            )
        )
        sink.ingest_billing_event(
            BillingEvent(
                provider=PROVIDER,
                event_id="evt_sub_cg2a",
                event_type="subscription.created",
                occurred_at=_now(),
                user_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status="active",
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
                event_type="subscription.renewed",
                occurred_at=_now(),
                user_id=USER_ID4,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status="active",
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        # customer created — ingest directly
        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_lifecycle",
                event_type="customer.created",
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
                    "next_billing_date": datetime.now(UTC).strftime(
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_dup",
                event_type="customer.created",
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_multi",
                event_type="customer.created",
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
        bs.sync_billing_from_config(BILLING_CONFIG)

        sink.ingest_billing_event(
            BillingEvent(
                provider="dodo",
                event_id="dodo:customer.created:cus_dodo_date",
                event_type="customer.created",
                occurred_at=_now(),
                user_id=USER_ID5,
                customer=BillingCustomerInfo(provider_customer_id="cus_dodo_date"),
            )
        )

        js_date = datetime.now(UTC).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        js_date_future = datetime.now(UTC).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")

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
