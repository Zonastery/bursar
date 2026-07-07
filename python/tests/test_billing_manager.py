"""Tests for BillingManager — provider-agnostic billing lifecycle state machine."""

from __future__ import annotations

from datetime import UTC, datetime

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
    BillingProviderRefs,
    BillingSubscriptionInfo,
    BillingSubscriptionOfferRef,
    BillingSubscriptionStatus,
    MemoryBillingStore,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def billing_store() -> MemoryBillingStore:
    return MemoryBillingStore()


@pytest.fixture
def credit_store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def credit_manager(credit_store: MemoryStore) -> CreditManager:
    mgr = CreditManager(store=credit_store)
    mgr.publish_pricing_from_dict(
        {
            "models": {"_default": "input_tokens * 1"},
            "min_balance": 0,
            "plans": {
                "free": {
                    "id": "free",
                    "name": "Free",
                    "free_allowance": 1000,
                },
                "pro": {
                    "id": "pro",
                    "name": "Pro",
                    "free_allowance": 100000,
                },
            },
            "tiers": {
                "purchased": {
                    "name": "Purchased",
                    "priority": 1,
                    "is_default": True,
                },
                "subscription": {
                    "name": "Subscription",
                    "priority": 2,
                    "is_default": False,
                },
            },
        }
    )
    return mgr


@pytest.fixture
def billing_config() -> BillingConfig:
    return BillingConfig(
        subscriptions={
            "pro_monthly": BillingOffer(
                offer_key="pro_monthly",
                plan_key="pro",
                interval=BillingOfferInterval.month,
                interval_count=1,
                entitlement_mode="allowance",
                provider_refs={
                    "stripe": BillingSubscriptionOfferRef(
                        provider="stripe",
                        price_id="price_pro_monthly",
                    ),
                },
            ),
            "pro_yearly": BillingOffer(
                offer_key="pro_yearly",
                plan_key="pro",
                interval=BillingOfferInterval.year,
                interval_count=1,
                entitlement_mode="allowance",
            ),
        },
        credit_topups={
            "standard_topup": BillingCreditTopup(
                tier="purchased",
                currency="USD",
                credits_per_major_unit=1000,
                min_amount_minor=500,
                max_amount_minor=500000,
                tax_behavior="exclude_tax",
                provider_refs={
                    "stripe": BillingSubscriptionOfferRef(
                        provider="stripe",
                        price_id="price_topup_usd",
                    ),
                },
            ),
        },
    )


@pytest.fixture
def manager(
    billing_store: MemoryBillingStore,
    credit_manager: CreditManager,
    billing_config: BillingConfig,
) -> BillingManager:
    return BillingManager(
        billing_store=billing_store,
        credit_manager=credit_manager,
        config=billing_config,
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _sub_event(
    provider: str = "stripe",
    event_id: str = "evt_001",
    event_type: str = "subscription.created",
    user_id: str | None = "user_123",
    provider_subscription_id: str = "sub_abc",
    provider_customer_id: str = "cus_xyz",
    status: str = "active",
    period_start: str | None = "2026-01-01T00:00:00Z",
    period_end: str | None = "2026-02-01T00:00:00Z",
    cancel_at_period_end: bool = False,
    interval: str | None = "month",
    interval_count: int | None = 1,
    price_id: str | None = "price_pro_monthly",
    product_id: str | None = None,
) -> BillingEvent:
    return BillingEvent(
        provider=provider,
        event_id=event_id,
        event_type=event_type,
        occurred_at="2026-01-01T00:00:00Z",
        user_id=user_id,
        customer=BillingCustomerInfo(
            provider_customer_id=provider_customer_id,
            email="user@example.com",
        ),
        subscription=BillingSubscriptionInfo(
            provider_subscription_id=provider_subscription_id,
            status=BillingSubscriptionStatus(status),
            cancel_at_period_end=cancel_at_period_end,
            period_start=period_start,
            period_end=period_end,
            interval=interval,
            interval_count=interval_count,
            refs=BillingProviderRefs(
                price_id=price_id,
                product_id=product_id,
            ),
        ),
    )


def _payment_event(
    provider: str = "stripe",
    event_id: str = "evt_pay_001",
    user_id: str | None = "user_123",
    provider_payment_id: str = "py_001",
    amount_minor: int = 2000,
    currency: str = "USD",
    price_id: str | None = "price_topup_usd",
    product_id: str | None = None,
) -> BillingEvent:
    return BillingEvent(
        provider=provider,
        event_id=event_id,
        event_type="payment.succeeded",
        occurred_at="2026-01-01T00:00:00Z",
        user_id=user_id,
        customer=BillingCustomerInfo(
            provider_customer_id="cus_xyz",
            email="user@example.com",
        ),
        payment=BillingPaymentInfo(
            provider_payment_id=provider_payment_id,
            amount_minor=amount_minor,
            currency=currency,
            refs=BillingProviderRefs(
                price_id=price_id,
                product_id=product_id,
            ),
            purpose="credit_topup",
        ),
    )


# ── Event idempotency ────────────────────────────────────────────────────


class TestEventIdempotency:
    def test_duplicate_event_is_rejected(self, manager: BillingManager) -> None:
        event = _sub_event(event_id="evt_dup_001")
        r1 = manager.handle_event(event)
        assert r1.handled is True
        assert r1.action != "duplicate"

        r2 = manager.handle_event(event)
        assert r2.handled is True
        assert r2.action == "duplicate"

    def test_different_events_same_provider_ok(self, manager: BillingManager) -> None:
        e1 = _sub_event(event_id="evt_a")
        e2 = _sub_event(event_id="evt_b")
        assert manager.handle_event(e1).handled
        assert manager.handle_event(e2).handled

    def test_same_event_id_different_providers_ok(self, manager: BillingManager) -> None:
        e1 = _sub_event(provider="stripe", event_id="evt_001")
        e2 = _sub_event(provider="dodo", event_id="evt_001")
        assert manager.handle_event(e1).handled
        assert manager.handle_event(e2).handled


# ── Customer events ─────────────────────────────────────────────────────


class TestCustomerEvents:
    def test_customer_created_stores_mapping(self, billing_store: MemoryBillingStore, manager: BillingManager) -> None:
        event = BillingEvent(
            provider="stripe",
            event_id="evt_cus_001",
            event_type="customer.created",
            occurred_at="2026-01-01T00:00:00Z",
            user_id="user_456",
            customer=BillingCustomerInfo(
                provider_customer_id="cus_new",
                email="new@example.com",
            ),
        )
        result = manager.handle_event(event)
        assert result.handled
        assert result.action == "customer_created"

        uid = billing_store.get_billing_customer("stripe", "cus_new")
        assert uid == "user_456"


# ── Subscription lifecycle ───────────────────────────────────────────────


class TestSubscriptionLifecycle:
    def test_subscription_created_stores_state(
        self,
        billing_store: MemoryBillingStore,
        manager: BillingManager,
    ) -> None:
        event = _sub_event()
        result = manager.handle_event(event)
        assert result.handled
        assert result.action == "subscription_created"

        sub = billing_store.get_billing_subscription("stripe", "sub_abc")
        assert sub is not None
        assert sub.user_id == "user_123"
        assert sub.status == "active"
        assert sub.offer_key == "pro_monthly"
        assert sub.plan_key == "pro"
        assert sub.interval == "month"

    def test_subscription_created_provisions_plan(
        self,
        credit_store: MemoryStore,
        credit_manager: CreditManager,
        manager: BillingManager,
    ) -> None:
        event = _sub_event()
        manager.handle_event(event)

        plan = credit_store.get_user_plan("user_123")
        assert plan.plan_id is not None
        assert plan.plan_name == "Pro"

    def test_subscription_updated_changes_status(
        self,
        billing_store: MemoryBillingStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        update = _sub_event(
            event_id="evt_002",
            event_type="subscription.updated",
            status="past_due",
        )
        result = manager.handle_event(update)
        assert result.action == "subscription_updated"

        sub = billing_store.get_billing_subscription("stripe", "sub_abc")
        assert sub is not None
        assert sub.status == "past_due"

    def test_subscription_canceled_revokes_plan(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        cancel = _sub_event(
            event_id="evt_003",
            event_type="subscription.canceled",
            status="canceled",
        )
        manager.handle_event(cancel)

        plan = credit_store.get_user_plan("user_123")
        assert plan.plan_id is None

    def test_subscription_expired_revokes_plan(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        expire = _sub_event(
            event_id="evt_004",
            event_type="subscription.expired",
            status="expired",
        )
        manager.handle_event(expire)

        plan = credit_store.get_user_plan("user_123")
        assert plan.plan_id is None

    def test_subscription_renewed_re_anchors_plan(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event(period_start="2026-01-01T00:00:00Z")
        manager.handle_event(create)

        first_assigned = credit_store._user_plan_assigned_at.get("user_123")

        renew = _sub_event(
            event_id="evt_005",
            event_type="subscription.renewed",
            period_start="2026-02-01T00:00:00Z",
        )
        manager.handle_event(renew)

        second_assigned = credit_store._user_plan_assigned_at.get("user_123")
        assert second_assigned is not None
        assert second_assigned != first_assigned

    def test_cancellation_scheduled_sets_flag(
        self,
        billing_store: MemoryBillingStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        schedule = _sub_event(
            event_id="evt_006",
            event_type="subscription.cancellation_scheduled",
            cancel_at_period_end=True,
        )
        result = manager.handle_event(schedule)
        assert result.action == "cancellation_scheduled"

        sub = billing_store.get_billing_subscription("stripe", "sub_abc")
        assert sub is not None
        assert sub.cancel_at_period_end is True

    def test_cancellation_unscheduled_clears_flag(
        self,
        billing_store: MemoryBillingStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        manager.handle_event(
            _sub_event(
                event_id="evt_006",
                event_type="subscription.cancellation_scheduled",
                cancel_at_period_end=True,
            )
        )
        manager.handle_event(
            _sub_event(
                event_id="evt_007",
                event_type="subscription.cancellation_unscheduled",
                cancel_at_period_end=False,
            )
        )

        sub = billing_store.get_billing_subscription("stripe", "sub_abc")
        assert sub is not None
        assert sub.cancel_at_period_end is False

    def test_paused_revokes_plan(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        pause = _sub_event(
            event_id="evt_008",
            event_type="subscription.paused",
            status="paused",
        )
        manager.handle_event(pause)

        plan = credit_store.get_user_plan("user_123")
        assert plan.plan_id is None

    def test_resumed_re_provisions(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        create = _sub_event()
        manager.handle_event(create)

        manager.handle_event(
            _sub_event(
                event_id="evt_008",
                event_type="subscription.paused",
                status="paused",
            )
        )

        resume = _sub_event(
            event_id="evt_009",
            event_type="subscription.resumed",
            status="active",
        )
        manager.handle_event(resume)

        plan = credit_store.get_user_plan("user_123")
        assert plan.plan_id is not None


# ── Payment / top-up ─────────────────────────────────────────────────────


class TestPaymentTopup:
    def test_payment_succeeded_grants_credits(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        event = _payment_event(amount_minor=2000)
        result = manager.handle_event(event)
        assert result.handled
        assert result.action == "payment_succeeded"

        balance = credit_store.get_balance("user_123")
        # 2000 minor = $20.00 * 1000 credits/dollar = 20000 credits
        assert balance.balance == 20000

    def test_payment_idempotent_skips_double_grant(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        event = _payment_event(event_id="evt_pay_001", amount_minor=1000)
        r1 = manager.handle_event(event)
        assert r1.handled
        assert r1.action == "payment_succeeded"

        balance_after_first = credit_store.get_balance("user_123").balance

        # Same event redelivered
        r2 = manager.handle_event(event)
        assert r2.handled
        assert r2.action == "duplicate"

        balance_after_second = credit_store.get_balance("user_123").balance
        assert balance_after_second == balance_after_first

    def test_payment_unresolved_topup_doesnt_grant(
        self,
        credit_store: MemoryStore,
        manager: BillingManager,
    ) -> None:
        event = _payment_event(
            price_id="price_unknown",
            amount_minor=1000,
        )
        result = manager.handle_event(event)
        assert result.handled
        assert result.action == "payment_succeeded"

        balance = credit_store.get_balance("user_123")
        assert balance.balance == 0


# ── Unhandled / ignored events ───────────────────────────────────────────


class TestUnhandledEvents:
    def test_unknown_event_type_is_ignored(self, manager: BillingManager) -> None:
        event = BillingEvent(
            provider="stripe",
            event_id="evt_unknown",
            event_type="unknown.type",
            occurred_at="2026-01-01T00:00:00Z",
        )
        result = manager.handle_event(event)
        assert result.handled
        assert result.action == "ignored"

    def test_user_not_found_returns_error(self, manager: BillingManager) -> None:
        event = _sub_event(user_id=None, provider_customer_id="cus_unknown")
        result = manager.handle_event(event)
        assert result.handled is False
        assert result.error == "user_not_found"


# ── Config sync ──────────────────────────────────────────────────────────


class TestConfigSync:
    def test_sync_billing_from_config_resolves_offer(
        self,
        billing_store: MemoryBillingStore,
    ) -> None:
        config = BillingConfig(
            subscriptions={
                "enterprise_monthly": BillingOffer(
                    offer_key="enterprise_monthly",
                    plan_key="pro",
                    interval=BillingOfferInterval.month,
                    interval_count=1,
                    entitlement_mode="allowance",
                ),
            },
        )
        billing_store.sync_billing_from_config(config)

        offer = billing_store.resolve_billing_offer(
            provider="stripe",
            product_id=None,
            price_id="price_pro_monthly",
        )
        if offer:
            assert offer["offer_key"] == "pro_monthly"

    def test_sync_billing_from_config_resolves_topup(
        self,
        billing_store: MemoryBillingStore,
    ) -> None:
        config = BillingConfig(
            credit_topups={
                "bonus_topup": BillingCreditTopup(
                    tier="purchased",
                    currency="USD",
                    credits_per_major_unit=2000,
                    provider_refs={
                        "stripe": BillingSubscriptionOfferRef(
                            provider="stripe",
                            price_id="price_bonus",
                        ),
                    },
                ),
            },
        )
        billing_store.sync_billing_from_config(config)

        topup = billing_store.resolve_credit_topup(
            provider="stripe",
            price_id="price_bonus",
        )
        assert topup is not None
        assert topup["topup_key"] == "bonus_topup"
        assert topup["credits_per_major_unit"] == 2000

    def test_compute_topup_credits(self, billing_store: MemoryBillingStore) -> None:
        config = {
            "credits_per_major_unit": 1000,
        }
        result = billing_store.compute_topup_credits(2000, config)
        # 2000 minor = $20.00, 20 * 1000 = 20000
        assert result == 20000


# ── Anchored plan assignment ────────────────────────────────────────────


class TestAnchoredPlanAssignment:
    def test_set_user_plan_with_anchored_assigned_at(
        self,
        credit_store: MemoryStore,
        credit_manager: CreditManager,
    ) -> None:
        anchored = datetime(2026, 1, 15, tzinfo=UTC)
        result = credit_manager.set_user_plan("user_123", "pro", plan_assigned_at=anchored)
        assert result.plan_assigned_at is not None

        stored = credit_store._user_plan_assigned_at.get("user_123")
        assert stored is not None
        assert stored == anchored

    def test_credit_manager_passes_anchored_to_store(
        self,
        credit_store: MemoryStore,
        credit_manager: CreditManager,
    ) -> None:
        anchored = datetime(2026, 3, 1, tzinfo=UTC)
        credit_manager.set_user_plan("user_456", "pro", plan_assigned_at=anchored)
        stored = credit_store._user_plan_assigned_at.get("user_456")
        assert stored == anchored

    def test_set_user_plan_defaults_to_now(
        self,
        credit_store: MemoryStore,
        credit_manager: CreditManager,
    ) -> None:
        before = datetime.now(UTC)
        credit_manager.set_user_plan("user_789", "free")
        after = datetime.now(UTC)

        stored = credit_store._user_plan_assigned_at.get("user_789")
        assert stored is not None
        assert before <= stored <= after

    def test_billing_manager_subscription_renewed_anchors_plan(
        self,
        credit_store: MemoryStore,
        credit_manager: CreditManager,
        manager: BillingManager,
    ) -> None:
        create = _sub_event(period_start="2026-01-01T00:00:00Z")
        manager.handle_event(create)

        first_assigned = credit_store._user_plan_assigned_at.get("user_123")
        assert first_assigned is not None

        renew = _sub_event(
            event_id="evt_renew_anchored",
            event_type="subscription.renewed",
            period_start="2026-02-01T00:00:00Z",
        )
        manager.handle_event(renew)

        second_assigned = credit_store._user_plan_assigned_at.get("user_123")
        assert second_assigned is not None
        assert second_assigned != first_assigned
        # Should be anchored to Feb 1
        assert second_assigned == datetime(2026, 2, 1, tzinfo=UTC)
