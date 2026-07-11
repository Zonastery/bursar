"""Integration tests for billing stores — MemoryBillingStore (always) and
PostgresBillingStore (when a real Postgres is available).

Each test exercises the full stored-procedure layer (resolve, claim,
complete, fail, upsert, get) to verify the SQL RPCs work identically to
the in-memory reference implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bursar import CreditManager, MemoryStore
from bursar.billing import (
    BillingConfig,
    BillingCreditTopup,
    BillingCustomerInfo,
    BillingEvent,
    BillingManager,
    BillingOffer,
    BillingOfferInterval,
    BillingPaymentInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CycleGrant,
    MemoryBillingStore,
    PostgresBillingStore,
    ProviderRef,
)

# ── Constants ─────────────────────────────────────────────────────────────

_USER_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID2 = "00000000-0000-0000-0000-000000000002"
_PROVIDER = "stripe"
_PROVIDER2 = "dodo"
_CUSTOMER_ID = "cus_test123"
_CUSTOMER_ID2 = "cus_test456"
_SUB_ID = "sub_test789"
_SUB_ID2 = "sub_test012"
_PRODUCT_ID = "prod_monthly"
_PRICE_ID = "price_monthly_1000"
_VARIANT_ID = "variant_monthly_1"
_PRICE_ID_TOPUP = "price_topup_credits"
_EVENT_ID = "evt_test_001"

_PRICING_DICT = {
    "version": 1,
    "metering": {
        "models": {"*": "input_tokens * 1"},
    },
    "ledger": {
        "min_balance": 0,
        "buckets": {
            "purchased": {"label": "Purchased", "priority": 1, "default": True, "allow_overdraft": False},
        },
    },
    "plans": {
        "free": {"label": "Free", "allowance": {"amount": 1000}},
        "pro": {"label": "Pro", "allowance": {"amount": 100000}},
        "enterprise": {"label": "Enterprise", "allowance": {"amount": 1000000}},
    },
}

_BILLING_CONFIG = BillingConfig(
    currency="USD",
    subscriptions={
        "pro_monthly": BillingOffer(
            plan="pro",
            interval=BillingOfferInterval.month,
            interval_count=1,
            providers={
                "stripe": ProviderRef(
                    product_id="prod_monthly",
                    price_id="price_monthly_1000",
                ),
            },
        ),
        "enterprise_yearly": BillingOffer(
            plan="enterprise",
            interval=BillingOfferInterval.year,
            interval_count=1,
            providers={
                "stripe": ProviderRef(
                    product_id="prod_yearly",
                    price_id="price_yearly_10000",
                ),
            },
        ),
        "cycle_grant_monthly": BillingOffer(
            plan="pro",
            interval=BillingOfferInterval.month,
            interval_count=1,
            grant=CycleGrant(
                mode="cycle_grant",
                credits=5000,
                bucket="purchased",
                replace_prior=True,
            ),
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
            deposit_to="purchased",
            credits_per_unit=1000,
            min_amount_minor=500,
            max_amount_minor=50000,
            providers={
                "stripe": ProviderRef(
                    product_id="prod_topup",
                    price_id="price_topup_credits",
                ),
            },
        ),
    },
)


# ── Parameterised store fixture ──────────────────────────────────────────


def _publish_pricing(cm: CreditManager) -> None:
    cm.publish_pricing_from_dict(_PRICING_DICT)


@pytest.fixture
def memory_components():
    """MemoryBillingStore + CreditManager (always available)."""
    cs = MemoryStore()
    cm = CreditManager(store=cs)
    _publish_publishing(cm)
    bs = MemoryBillingStore()
    bs.sync_billing_from_config(_BILLING_CONFIG)
    bm = BillingManager(bs, credit_manager=cm, config=_BILLING_CONFIG)
    return cs, cm, bs, bm


@pytest.fixture
def pg_components(pg_database_url: str):
    """PostgresBillingStore + CreditManager (requires real Postgres)."""
    # Ensure test users exist in auth.users (FK target)
    from psycopg2 import connect

    from bursar.interface.postgres import PostgresStore

    conn = connect(pg_database_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        for uid in [_USER_ID, _USER_ID2]:
            cur.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING", [uid])
    conn.close()

    cs = PostgresStore(pg_database_url)
    cs.setup()
    cm = CreditManager(store=cs)
    _publish_publishing(cm)
    bs = PostgresBillingStore(pg_database_url)
    bs.sync_billing_from_config(_BILLING_CONFIG)
    bm = BillingManager(bs, credit_manager=cm, config=_BILLING_CONFIG)
    return cs, cm, bs, bm


# Small helper due to bug in the test function name...
def _publish_publishing(cm: CreditManager) -> None:
    cm.publish_pricing_from_dict(_PRICING_DICT)


def _make_event(
    event_id: str = _EVENT_ID,
    event_type: str = "customer.created",
    user_id: str | None = _USER_ID,
    provider: str = _PROVIDER,
    customer_id: str | None = _CUSTOMER_ID,
    sub_id: str | None = None,
) -> BillingEvent:
    return BillingEvent(
        provider=provider,
        event_id=event_id,
        event_type=event_type,
        occurred_at=datetime.now(UTC).isoformat(),
        user_id=user_id,
        customer=BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None,
    )


# ── Test class ────────────────────────────────────────────────────────────


class TestMemoryBillingStoreIntegration:
    """MemoryBillingStore integration — always runs, no DB needed."""

    @pytest.fixture
    def components(self):
        cs = MemoryStore()
        cm = CreditManager(store=cs)
        _publish_publishing(cm)
        bs = MemoryBillingStore()
        bs.sync_billing_from_config(_BILLING_CONFIG)
        bm = BillingManager(bs, credit_manager=cm, config=_BILLING_CONFIG)
        return cs, cm, bs, bm

    def test_sync_billing_config_roundtrip(self, components):
        _, _, bs, _ = components
        offer = bs.resolve_billing_offer(_PROVIDER, price_id=_PRICE_ID)
        assert offer is not None
        assert offer["offer_key"] == "pro_monthly"
        assert offer["plan"] == "pro"

    def test_sync_billing_config_resolve_by_product_id(self, components):
        _, _, bs, _ = components
        offer = bs.resolve_billing_offer(_PROVIDER, product_id="prod_monthly")
        assert offer is not None
        assert offer["offer_key"] == "pro_monthly"

    def test_sync_topup_config_roundtrip(self, components):
        _, _, bs, _ = components
        topup = bs.resolve_credit_topup(_PROVIDER, price_id=_PRICE_ID_TOPUP)
        assert topup is not None
        assert topup["topup_key"] == "standard_topup"
        assert topup["credits_per_unit"] == 1000

    def test_unresolved_offer_returns_none(self, components):
        _, _, bs, _ = components
        assert bs.resolve_billing_offer(_PROVIDER, price_id="nonexistent") is None

    def test_customer_created_roundtrip(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID, "test@example.com")
        uid = bs.get_billing_customer(_PROVIDER, _CUSTOMER_ID)
        assert uid == _USER_ID

    def test_customer_updated_replaces_user_id(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID)
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID2)
        uid = bs.get_billing_customer(_PROVIDER, _CUSTOMER_ID)
        assert uid == _USER_ID2

    def test_event_idempotency(self, components):
        _, _, bs, bm = components
        event = _make_event(event_type="customer.created")
        r1 = bm.handle_event(event)
        assert r1.handled
        r2 = bm.handle_event(event)
        assert r2.action == "duplicate"

    def test_event_claim_complete_fail_cycle(self, components):
        _, _, bs, _ = components
        claim1 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim1.status == "claimed"
        bs.complete_billing_event(_PROVIDER, _EVENT_ID)
        claim2 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim2.status == "duplicate"

    def test_event_fail_then_reclaim(self, components):
        _, _, bs, _ = components
        claim1 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim1.status == "claimed"
        bs.fail_billing_event(_PROVIDER, _EVENT_ID)
        claim2 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim2.status == "claimed"

    def test_multiple_providers_same_customer_id(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer("stripe", _CUSTOMER_ID, _USER_ID)
        bs.upsert_billing_customer("dodo", _CUSTOMER_ID, _USER_ID2)
        assert bs.get_billing_customer("stripe", _CUSTOMER_ID) == _USER_ID
        assert bs.get_billing_customer("dodo", _CUSTOMER_ID) == _USER_ID2

    def test_subscription_upsert_and_read(self, components):
        _, _, bs, _ = components
        state = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            provider_customer_id=_CUSTOMER_ID,
            offer_key="pro_monthly",
            plan="pro",
            status="active",
            current_period_start="2025-01-01T00:00:00Z",
            current_period_end="2025-02-01T00:00:00Z",
        )
        bs.upsert_billing_subscription(state)
        result = bs.get_billing_subscription(_PROVIDER, _SUB_ID)
        assert result is not None
        assert result.user_id == _USER_ID
        assert result.status == "active"
        assert result.plan == "pro"

    def test_subscription_update(self, components):
        _, _, bs, _ = components
        state = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            status="active",
        )
        bs.upsert_billing_subscription(state)
        updated = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            status="canceled",
        )
        bs.upsert_billing_subscription(updated)
        result = bs.get_billing_subscription(_PROVIDER, _SUB_ID)
        assert result is not None
        assert result.status == "canceled"

    def test_compute_topup_credits(self, components):
        config = {"credits_per_major_unit": 1000}
        credits = BillingManager._compute_topup_credits(2000, config)
        assert credits == 20000  # (2000 / 100) * 1000 = 20000

    def test_compute_topup_credits_odd_amount(self, components):
        config = {"credits_per_major_unit": 1000}
        credits = BillingManager._compute_topup_credits(1999, config)
        assert credits == 19990  # int(19.99 * 1000) = 19990

    def test_resolve_billing_offer_no_match(self, components):
        _, _, bs, _ = components
        assert bs.resolve_billing_offer("nonexistent_provider", price_id=_PRICE_ID) is None

    def test_subscription_not_found(self, components):
        _, _, bs, _ = components
        assert bs.get_billing_subscription(_PROVIDER, "nonexistent_sub") is None

    def test_customer_not_found(self, components):
        _, _, bs, _ = components
        assert bs.get_billing_customer(_PROVIDER, "nonexistent_cus") is None

    def test_sync_offers_replaces_all(self, components):
        _, _, bs, _ = components
        config = BillingConfig(
            subscriptions={
                "new_offer": BillingOffer(
                    plan="free",
                    interval=BillingOfferInterval.month,
                    providers={
                        "stripe": ProviderRef(
                            price_id="price_new_offer",
                        ),
                    },
                ),
            },
        )
        bs.sync_billing_from_config(config)
        offer = bs.resolve_billing_offer(_PROVIDER, price_id=_PRICE_ID)
        assert offer is None, "Old offers not resolvable after re-sync (Memory clears all)"
        new_offer = bs.resolve_billing_offer("stripe", price_id="price_new_offer")
        assert new_offer is not None
        assert new_offer["offer_key"] == "new_offer"


class TestPostgresBillingStoreIntegration:
    """PostgresBillingStore integration — requires real Postgres (DATABASE_URL)."""

    @pytest.fixture
    def components(self, pg_database_url: str):
        from psycopg2 import connect

        from bursar.interface.postgres import PostgresStore

        conn = connect(pg_database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            for uid in [_USER_ID, _USER_ID2]:
                cur.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING", [uid])
        conn.close()

        cs = PostgresStore(pg_database_url)
        cs.setup()
        cm = CreditManager(store=cs)
        _publish_publishing(cm)
        bs = PostgresBillingStore(pg_database_url)
        bs.sync_billing_from_config(_BILLING_CONFIG)
        bm = BillingManager(bs, credit_manager=cm, config=_BILLING_CONFIG)
        return cs, cm, bs, bm

    # ── Sync + Resolve ───────────────────────────────────────────────────

    def test_sync_billing_config_roundtrip(self, components):
        _, _, bs, _ = components
        offer = bs.resolve_billing_offer(_PROVIDER, price_id=_PRICE_ID)
        assert offer is not None
        assert offer["offer_key"] == "pro_monthly"
        assert offer["plan"] == "pro"

    def test_sync_billing_config_resolve_by_product_id(self, components):
        _, _, bs, _ = components
        offer = bs.resolve_billing_offer(_PROVIDER, product_id="prod_monthly")
        assert offer is not None
        assert offer["offer_key"] == "pro_monthly"

    def test_sync_topup_config_roundtrip(self, components):
        _, _, bs, _ = components
        topup = bs.resolve_credit_topup(_PROVIDER, price_id=_PRICE_ID_TOPUP)
        assert topup is not None
        assert topup["topup_key"] == "standard_topup"
        assert topup["credits_per_unit"] == 1000

    def test_unresolved_offer_returns_none(self, components):
        _, _, bs, _ = components
        assert bs.resolve_billing_offer(_PROVIDER, price_id="nonexistent") is None

    def test_resolve_billing_offer_no_match(self, components):
        _, _, bs, _ = components
        assert bs.resolve_billing_offer("nonexistent_provider", price_id=_PRICE_ID) is None

    # ── Customer CRUD ────────────────────────────────────────────────────

    def test_customer_created_roundtrip(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID, "test@example.com")
        uid = bs.get_billing_customer(_PROVIDER, _CUSTOMER_ID)
        assert uid == _USER_ID

    def test_customer_not_found(self, components):
        _, _, bs, _ = components
        assert bs.get_billing_customer(_PROVIDER, "nonexistent_cus") is None

    def test_customer_updated_replaces_user_id(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID)
        bs.upsert_billing_customer(_PROVIDER, _CUSTOMER_ID, _USER_ID2)
        uid = bs.get_billing_customer(_PROVIDER, _CUSTOMER_ID)
        assert uid == _USER_ID2

    def test_multiple_providers_same_customer_id(self, components):
        _, _, bs, _ = components
        bs.upsert_billing_customer("stripe", _CUSTOMER_ID, _USER_ID)
        bs.upsert_billing_customer("dodo", _CUSTOMER_ID, _USER_ID2)
        assert bs.get_billing_customer("stripe", _CUSTOMER_ID) == _USER_ID
        assert bs.get_billing_customer("dodo", _CUSTOMER_ID) == _USER_ID2

    # ── Subscription CRUD ────────────────────────────────────────────────

    def test_subscription_upsert_and_read(self, components):
        _, _, bs, _ = components
        state = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            provider_customer_id=_CUSTOMER_ID,
            offer_key="pro_monthly",
            plan="pro",
            status="active",
            current_period_start="2025-01-01T00:00:00Z",
            current_period_end="2025-02-01T00:00:00Z",
        )
        bs.upsert_billing_subscription(state)
        result = bs.get_billing_subscription(_PROVIDER, _SUB_ID)
        assert result is not None
        assert result.user_id == _USER_ID
        assert result.status == "active"
        assert result.plan == "pro"

    def test_subscription_not_found(self, components):
        _, _, bs, _ = components
        assert bs.get_billing_subscription(_PROVIDER, "nonexistent_sub") is None

    def test_subscription_update(self, components):
        _, _, bs, _ = components
        state = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            status="active",
        )
        bs.upsert_billing_subscription(state)
        updated = BillingSubscriptionState(
            user_id=_USER_ID,
            provider=_PROVIDER,
            provider_subscription_id=_SUB_ID,
            status="canceled",
        )
        bs.upsert_billing_subscription(updated)
        result = bs.get_billing_subscription(_PROVIDER, _SUB_ID)
        assert result is not None
        assert result.status == "canceled"

    # ── Event idempotency ────────────────────────────────────────────────

    def test_event_idempotency(self, components):
        _, _, bs, bm = components
        event = _make_event(event_type="customer.created")
        r1 = bm.handle_event(event)
        assert r1.handled
        r2 = bm.handle_event(event)
        assert r2.action == "duplicate"

    def test_event_claim_complete_fail_cycle(self, components):
        _, _, bs, _ = components
        claim1 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim1.status == "claimed"
        bs.complete_billing_event(_PROVIDER, _EVENT_ID)
        claim2 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim2.status == "duplicate"

    def test_event_fail_then_reclaim(self, components):
        _, _, bs, _ = components
        claim1 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim1.status == "claimed"
        bs.fail_billing_event(_PROVIDER, _EVENT_ID)
        claim2 = bs.claim_billing_event(_PROVIDER, _EVENT_ID, "test.event")
        assert claim2.status == "claimed"

    # ── Topup credits ────────────────────────────────────────────────────

    def test_compute_topup_credits(self, components):
        config = {"credits_per_major_unit": 1000}
        credits = BillingManager._compute_topup_credits(2000, config)
        assert credits == 20000  # (2000 / 100) * 1000 = 20000

    def test_compute_topup_credits_odd_amount(self, components):
        config = {"credits_per_major_unit": 1000}
        credits = BillingManager._compute_topup_credits(1999, config)
        assert credits == 19990  # int(19.99 * 1000) = 19990

    # ── BillingManager lifecycle ─────────────────────────────────────────

    def test_subscription_lifecycle_full(self, components):
        _, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_customer_1",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID),
                subscription=None,
            )
        )
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_create_1",
                event_type="subscription.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=_SUB_ID,
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(product_id=_PRODUCT_ID, price_id=_PRICE_ID),
                    interval="month",
                    interval_count=1,
                ),
            )
        )
        plan = cm.get_user_plan(_USER_ID)
        # PostgresStore returns UUID plan_id, not plan_key — just check assigned
        assert plan.plan_id is not None
        assert plan.plan_assigned_at is not None

        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_cancel_1",
                event_type="subscription.canceled",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=_SUB_ID, status=BillingSubscriptionStatus.canceled
                ),
            )
        )
        plan = cm.get_user_plan(_USER_ID)
        assert plan.plan_id is None

    def test_topup_credit_grant(self, components):
        _, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_customer_2",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
            )
        )
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_payment_2",
                event_type="payment.succeeded",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
                payment=BillingPaymentInfo(
                    provider_payment_id="py_test456",
                    amount_minor=2000,
                    currency="USD",
                    refs=ProviderRef(product_id="prod_topup", price_id=_PRICE_ID_TOPUP),
                    purpose="credit_topup",
                ),
            )
        )
        balance = cm.get_balance(_USER_ID2)
        assert balance.balance == Decimal("20000")

    def test_subscription_pause_resume(self, components):
        _, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_cus_pause",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
            )
        )
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_pause_1",
                event_type="subscription.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=_SUB_ID2,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=_PRODUCT_ID, price_id=_PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(_USER_ID2).plan_id is not None
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_pause_2",
                event_type="subscription.paused",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(provider_subscription_id=_SUB_ID2),
            )
        )
        assert cm.get_user_plan(_USER_ID2).plan_id is None
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_pause_3",
                event_type="subscription.resumed",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=_SUB_ID2,
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(product_id=_PRODUCT_ID, price_id=_PRICE_ID),
                ),
            )
        )
        assert cm.get_user_plan(_USER_ID2).plan_id is not None

    def test_unknown_event_type_is_failed(self, components):
        _, _, bs, bm = components
        event = BillingEvent(
            provider=_PROVIDER,
            event_id="evt_unknown",
            event_type="some.unknown.event",
            occurred_at=datetime.now(UTC).isoformat(),
            user_id=_USER_ID,
        )
        result = bm.handle_event(event)
        assert not result.handled
        assert result.error == "unhandled_event_type"

    def test_duplicate_event_skips_side_effects(self, components):
        _, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_cus_dup",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        assert bs.get_billing_customer(_PROVIDER, "cus_dup_test") == _USER_ID
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_cus_dup",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id="cus_dup_test"),
            )
        )
        result = bs.get_billing_customer(_PROVIDER, "cus_dup_test")
        assert result == _USER_ID, "Duplicate should NOT update the customer mapping"

    def test_provider_scoped_event_id(self, components):
        _, _, bs, _ = components
        c1 = bs.claim_billing_event("stripe", _EVENT_ID, "test.event")
        assert c1.status == "claimed"
        c2 = bs.claim_billing_event("dodo", _EVENT_ID, "test.event")
        assert c2.status == "claimed"

    def test_sync_offers_adds_new(self, components):
        _, _, bs, _ = components
        config = BillingConfig(
            subscriptions={
                "new_offer": BillingOffer(
                    plan="free",
                    interval=BillingOfferInterval.month,
                    providers={
                        "stripe": ProviderRef(
                            price_id="price_new_offer",
                        ),
                    },
                ),
            },
        )
        bs.sync_billing_from_config(config)
        new_offer = bs.resolve_billing_offer("stripe", price_id="price_new_offer")
        assert new_offer is not None
        assert new_offer["offer_key"] == "new_offer"

    def test_cycle_grant_credits_granted(self, components):
        cs, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_cus_cg1",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
            )
        )
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_cg1",
                event_type="subscription.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID2,
                customer=BillingCustomerInfo(provider_customer_id=_CUSTOMER_ID2),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_test",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(product_id="prod_cycle_grant", price_id="price_cycle_grant_5000"),
                ),
            )
        )
        balance = cm.get_balance(_USER_ID2)
        assert balance.balance == Decimal("5000")

    def test_cycle_grant_replace_prior(self, components):
        cs, cm, bs, bm = components
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_cus_cg2",
                event_type="customer.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
            )
        )
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_cg2a",
                event_type="subscription.created",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-06-01T00:00:00Z",
                    period_end="2025-07-01T00:00:00Z",
                    refs=ProviderRef(product_id="prod_cycle_grant", price_id="price_cycle_grant_5000"),
                ),
            )
        )
        balance1 = cm.get_balance(_USER_ID)
        assert balance1.balance == Decimal("5000")

        # Renew — should revoke prior cycle_grant and grant new 5000
        bm.handle_event(
            BillingEvent(
                provider=_PROVIDER,
                event_id="evt_sub_cg2b",
                event_type="subscription.renewed",
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=_USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="cus_cg_replace"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="sub_cg_replace",
                    status=BillingSubscriptionStatus.active,
                    period_start="2025-07-01T00:00:00Z",
                    period_end="2025-08-01T00:00:00Z",
                    refs=ProviderRef(product_id="prod_cycle_grant", price_id="price_cycle_grant_5000"),
                ),
            )
        )
        balance2 = cm.get_balance(_USER_ID)
        assert balance2.balance == Decimal("5000")


class TestPricingConfigPreservesBillingRefs:
    """Publishing pricing must NOT wipe billing provider refs (B1)."""

    @pytest.fixture
    def components(self, pg_database_url: str):
        from psycopg2 import connect

        from bursar.interface.postgres import PostgresStore

        conn = connect(pg_database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING", [_USER_ID])
        conn.close()

        cs = PostgresStore(pg_database_url)
        cs.setup()
        cm = CreditManager(store=cs)

        bs = PostgresBillingStore(pg_database_url)
        bs.sync_billing_from_config(_BILLING_CONFIG)

        cm.publish_pricing_from_dict(_PRICING_DICT)

        return cs, cm, bs

    def test_offer_resolvable_after_pricing_publish(self, components):
        """Verify billing offers survive publish_pricing_from_dict."""
        _, _, bs = components
        offer = bs.resolve_billing_offer(_PROVIDER, price_id=_PRICE_ID)
        assert offer is not None
        assert offer["offer_key"] == "pro_monthly"

    def test_topup_resolvable_after_pricing_publish(self, components):
        """Verify credit topups survive publish_pricing_from_dict."""
        _, _, bs = components
        topup = bs.resolve_credit_topup(_PROVIDER, price_id=_PRICE_ID_TOPUP)
        assert topup is not None
        assert topup["topup_key"] == "standard_topup"
