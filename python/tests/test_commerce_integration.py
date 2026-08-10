"""DB-backed commerce integration tests for the Python SDK."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg2
import pytest

from bursar.billing.contracts import AutoRechargeProviderPaymentUpdate, CheckoutIntentUpdate
from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.types import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingPaymentInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.bursar import Bursar
from bursar.commerce import AutoRechargeInput, CheckoutConflictError, CommerceOptions, CreateCheckoutInput
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.types import (
    ChangePlanPreview,
    CheckoutParams,
    CheckoutSessionResult,
    PaymentMethodInfo,
    PreviewChangePlanParams,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
)
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]

USER_ID = "00000000-0000-0000-0000-000000000001"
USER_ID2 = "00000000-0000-0000-0000-000000000002"
USER_ID3 = "00000000-0000-0000-0000-000000000003"
CUSTOMER_ID = "cus_commerce_1"
CUSTOMER_ID2 = "cus_commerce_2"
CUSTOMER_ID3 = "cus_commerce_3"


class IntegrationMockProvider(MockPaymentProvider):
    provider = "stripe"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.checkout_params: list[CheckoutParams] = []
        self.checkout_gate: asyncio.Event | None = None
        self.charges: list[SavedPaymentChargeResult] = [
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_processing",
                status="processing",
                amount_minor=500,
                currency="USD",
            ),
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_action",
                status="requires_customer_action",
                action_url="https://app.example/confirm",
                amount_minor=500,
                currency="USD",
            ),
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_failed",
                status="failed",
                amount_minor=500,
                currency="USD",
            ),
        ]

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        self.checkout_params.append(params)
        if self.checkout_gate is not None:
            if len(self.checkout_params) == 2:
                self.checkout_gate.set()
            await self.checkout_gate.wait()
        return CheckoutSessionResult(
            url=params.return_url,
            provider_session_id=f"session_{params.idempotency_key}",
            customer_id=CUSTOMER_ID,
        )

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        del params
        return ChangePlanPreview(
            total_amount=0,
            settlement_amount=0,
            currency="USD",
            line_items=[],
            effective_at="2026-08-01T00:00:00+00:00",
            next_billing_date="2026-09-01T00:00:00+00:00",
        )

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        del customer_id
        return [
            PaymentMethodInfo(
                id="pm_card_visa",
                last4="4242",
                brand="visa",
                expiry_month=12,
                expiry_year=2030,
                is_default=True,
            )
        ]

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        del params
        return SavedPaymentChargeQuote(amount_minor=500, currency="USD")

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        del params
        return (
            self.charges.pop(0)
            if self.charges
            else SavedPaymentChargeResult(
                provider_payment_id="auto_pay_success",
                status="succeeded",
                amount_minor=500,
                currency="USD",
            )
        )


CONFIG: dict[str, Any] = {
    "version": 1,
    "catalog": {"default_plan": "starter"},
    "pricing": {
        "operations": {
            "completion": {
                "measures": {"tokens": {"unit": "token"}},
                "dimensions": {},
            }
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "completion": {
                        "rules": [],
                        "unmatched": {
                            "action": "charge",
                            "charge": {"type": "per_unit", "measure": "tokens", "rate": "1"},
                        },
                    }
                }
            }
        },
    },
    "credits": {
        "buckets": {"general": {"priority": 10}},
        "default_bucket": "general",
        "policies": {"prepaid": {"type": "prepaid"}},
    },
    "entitlements": {"features": {}},
    "admission": {"policies": {}},
    "plans": {
        "starter": {
            "display_name": "Starter",
            "rank": 0,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "features": {},
            "quotas": {},
            "credit_policy": "prepaid",
        },
        "pro": {
            "display_name": "Pro",
            "rank": 1,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "features": {},
            "quotas": {},
            "credit_policy": "prepaid",
        },
    },
    "commerce": {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "starter_month": {
                "type": "subscription",
                "display_name": "Starter monthly",
                "plan": "starter",
                "billing_interval": {"unit": "month", "count": 1},
                "price": {"amount_minor": 1000, "currency": "USD"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_starter_month"}},
            },
            "pro_month": {
                "type": "subscription",
                "display_name": "Pro monthly",
                "plan": "pro",
                "billing_interval": {"unit": "month", "count": 1},
                "price": {"amount_minor": 2000, "currency": "USD"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_pro_month"}},
            },
            "standard_topup": {
                "type": "topup",
                "display_name": "Standard top-up",
                "credits_per_unit": "100",
                "quantity": {"minimum": 1, "maximum": 5, "default": 1},
                "bucket": "general",
                "price": {"amount_minor": 500, "currency": "USD"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_topup_500"}},
            },
        },
        "subscription_changes": {
            "upgrade": {"effective": "immediate", "proration": "prorated", "payment_failure": "prevent_change"},
            "downgrade": {"effective": "renewal", "proration": "none", "payment_failure": "prevent_change"},
            "lateral": {"effective": "immediate", "proration": "prorated", "payment_failure": "prevent_change"},
            "cadence_change": {"effective": "renewal", "proration": "none", "payment_failure": "prevent_change"},
        },
        "auto_recharge": {
            "eligible_topups": ["standard_topup"],
            "balance_below": {"minimum": "100", "maximum": "5000", "default": "1000"},
            "rearm_above": "6000",
            "quantity": {"minimum": 1, "maximum": 3, "default": 1},
            "limits": {
                "max_purchases": 3,
                "window": {
                    "type": "rolling",
                    "duration": {"unit": "day", "count": 30},
                },
                "max_charge_minor": 1500,
                "cooldown": {"unit": "hour", "count": 1},
            },
        },
    },
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bursar(pg_database_url: str, pg_store: object) -> tuple[Bursar, PostgresBillingStore, IntegrationMockProvider]:
    billing_store = PostgresBillingStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test")
    provider = IntegrationMockProvider(event_sink=None)  # type: ignore[arg-type]
    bursar = Bursar(
        credit_store=pg_store,  # type: ignore[arg-type]
        billing_store=billing_store,
        commerce_options=CommerceOptions(provider_environment="test", providers={"stripe": lambda context: provider}),
    )
    bursar.catalog.publish_and_activate(CONFIG)
    return bursar, billing_store, provider


@pytest.mark.asyncio
async def test_commerce_checkout_persists_intent_and_topup_payment_grants_credits(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, _provider = _bursar(pg_database_url, pg_store)
    try:
        assert bursar.commerce is not None
        checkout = await bursar.commerce.create_checkout(
            CreateCheckoutInput(
                subject_id=USER_ID,
                account_id=USER_ID,
                offer_key="standard_topup",
                type="credit_pack",
                return_url="https://app.example/return?intent={intentId}",
                cancel_url="https://app.example/cancel?intent={intentId}",
                operation_key="checkout-topup-1",
            )
        )

        assert checkout.provider == "stripe"
        assert checkout.intent_id in checkout.url
        assert bursar.commerce.get_checkout_status(checkout.intent_id, USER_ID).status == "pending"

        result = bursar.ingest_billing_event(
            BillingEvent(
                provider="stripe",
                event_id="evt_topup_paid",
                event_type=BillingEventType.payment_succeeded,
                occurred_at=_now(),
                account_id=USER_ID,
                payment=BillingPaymentInfo(
                    provider_payment_id="pay_topup_1",
                    amount_minor=500,
                    tax_minor=0,
                    currency="USD",
                    refs=ProviderRef(price_id="price_topup_500"),
                    purpose="credit_topup",
                    status="succeeded",
                ),
            )
        )

        assert result.handled is True
        assert bursar.credits.get_balance(USER_ID).balance == Decimal("100")
        overview = await bursar.commerce.get_account_overview(USER_ID)
        assert overview.credits.ledger_balance == Decimal("100")
        assert overview.transactions[0].entry_type == "purchase"
    finally:
        billing_store.close()


@pytest.mark.asyncio
async def test_checkout_operation_key_replays_once_and_conflicts_before_provider(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, provider = _bursar(pg_database_url, pg_store)
    try:
        commerce = bursar.commerce
        assert commerce is not None
        base = {
            "subject_id": USER_ID,
            "account_id": USER_ID,
            "offer_key": "standard_topup",
            "quantity": 1,
            "return_url": "https://app.example/return?intent={intentId}",
            "cancel_url": "https://app.example/cancel?intent={intentId}",
            "operation_key": "checkout-operation-replay",
        }

        async def checkout(**overrides: object):
            return await commerce.create_checkout(CreateCheckoutInput.model_validate({**base, **overrides}))

        first = await checkout()
        assert await checkout() == first
        assert len(provider.checkout_params) == 1

        for changed in (
            {"account_id": USER_ID2},
            {"quantity": 2},
            {"offer_key": "pro_month", "quantity": 1},
        ):
            with pytest.raises(CheckoutConflictError):
                await checkout(**changed)
        assert len(provider.checkout_params) == 1

        independent = await checkout(operation_key="checkout-operation-independent")
        assert independent.intent_id != first.intent_id
        assert len(provider.checkout_params) == 2

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT operation_key,
                       encode(request_digest, 'hex'),
                       count(*)
                FROM bursar.billing_checkout_intents
                WHERE subject_id = %s::uuid
                GROUP BY operation_key, request_digest
                ORDER BY operation_key
                """,
                (USER_ID,),
            )
            assert cursor.fetchall() == [
                (
                    "checkout-operation-independent",
                    "685d7d08850ce95a0ce0a59dacba601f7b1dcaea192d215aa147319a74c628f3",
                    1,
                ),
                (
                    "checkout-operation-replay",
                    "685d7d08850ce95a0ce0a59dacba601f7b1dcaea192d215aa147319a74c628f3",
                    1,
                ),
            ]
    finally:
        billing_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "operation_key", "failure_message"),
    [
        ("intent", "checkout-intent-persistence-recovery", "injected checkout persistence failure"),
        ("customer", "checkout-customer-persistence-recovery", "injected customer persistence failure"),
    ],
)
async def test_checkout_recovers_open_intent_after_transient_persistence_failure(
    pg_database_url: str,
    pg_store: object,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    operation_key: str,
    failure_message: str,
) -> None:
    bursar, billing_store, provider = _bursar(pg_database_url, pg_store)
    original_update = billing_store.update_checkout_intent
    original_upsert = billing_store.upsert_billing_customer
    fail_once = True

    def flaky_update(intent_id: str, update: CheckoutIntentUpdate) -> None:
        nonlocal fail_once
        if failure_point == "intent" and fail_once:
            fail_once = False
            raise RuntimeError(failure_message)
        original_update(intent_id, update)

    def flaky_upsert(provider_name: str, customer_id: str, user_id: str, email: str | None = None) -> None:
        nonlocal fail_once
        if failure_point == "customer" and fail_once:
            fail_once = False
            raise RuntimeError(failure_message)
        original_upsert(provider_name, customer_id, user_id, email)

    monkeypatch.setattr(billing_store, "update_checkout_intent", flaky_update)
    monkeypatch.setattr(billing_store, "upsert_billing_customer", flaky_upsert)
    try:
        assert bursar.commerce is not None
        input = CreateCheckoutInput(
            subject_id=USER_ID,
            account_id=USER_ID,
            offer_key="standard_topup",
            return_url="https://app.example/return?intent={intentId}",
            cancel_url="https://app.example/cancel?intent={intentId}",
            operation_key=operation_key,
        )

        with pytest.raises(RuntimeError, match=failure_message):
            await bursar.commerce.create_checkout(input)
        assert len(provider.checkout_params) == 1

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text, status, provider_session_id, checkout_url
                FROM bursar.billing_checkout_intents
                WHERE subject_id = %s::uuid
                  AND operation_key = %s
                """,
                (USER_ID, input.operation_key),
            )
            after_failure = cursor.fetchall()
            cursor.execute(
                "SELECT count(*) FROM bursar.billing_customers WHERE subject_id = %s::uuid",
                (USER_ID,),
            )
            customer_count_row = cursor.fetchone()
            assert customer_count_row is not None
            customer_count_after_failure = customer_count_row[0]
        assert len(after_failure) == 1
        intent_id, status, provider_session_id, checkout_url = after_failure[0]
        assert (status, provider_session_id, checkout_url) == ("open", None, None)
        assert customer_count_after_failure == (1 if failure_point == "intent" else 0)

        recovered = await bursar.commerce.create_checkout(input)
        assert recovered.intent_id == intent_id
        assert len(provider.checkout_params) == 2
        assert [params.idempotency_key for params in provider.checkout_params] == [
            input.operation_key,
            input.operation_key,
        ]
        assert {params.return_url for params in provider.checkout_params} == {recovered.url}

        original_update(recovered.intent_id, CheckoutIntentUpdate(status="completed"))
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, provider_session_id, checkout_url
                FROM bursar.billing_checkout_intents
                WHERE id = %s::uuid
                """,
                (recovered.intent_id,),
            )
            assert cursor.fetchone() == (
                "completed",
                f"session_{input.operation_key}",
                recovered.url,
            )
            cursor.execute(
                """
                SELECT min(provider_customer_id), count(*)
                FROM bursar.billing_customers
                WHERE subject_id = %s::uuid
                """,
                (USER_ID,),
            )
            assert cursor.fetchone() == (CUSTOMER_ID, 1)
    finally:
        billing_store.close()


@pytest.mark.asyncio
async def test_concurrent_same_key_checkouts_converge_on_one_provider_session(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, provider = _bursar(pg_database_url, pg_store)
    provider.checkout_gate = asyncio.Event()
    try:
        commerce = bursar.commerce
        assert commerce is not None
        input = CreateCheckoutInput(
            subject_id=USER_ID,
            account_id=USER_ID,
            offer_key="standard_topup",
            return_url="https://app.example/return?intent={intentId}",
            cancel_url="https://app.example/cancel?intent={intentId}",
            operation_key="checkout-concurrent-replay",
        )

        first, second = await asyncio.gather(
            commerce.create_checkout(input),
            commerce.create_checkout(input),
        )

        assert second == first
        assert len(provider.checkout_params) == 2
        assert {params.idempotency_key for params in provider.checkout_params} == {input.operation_key}
        assert {params.return_url for params in provider.checkout_params} == {first.url}
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT min(id::text),
                       min(status),
                       min(provider_session_id),
                       min(checkout_url),
                       count(*)
                FROM bursar.billing_checkout_intents
                WHERE subject_id = %s::uuid
                  AND operation_key = %s
                """,
                (USER_ID, input.operation_key),
            )
            assert cursor.fetchone() == (
                first.intent_id,
                "open",
                f"session_{input.operation_key}",
                first.url,
                1,
            )
    finally:
        billing_store.close()


@pytest.mark.asyncio
async def test_commerce_subscription_plan_change_portal_and_cancel_flow(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, _provider = _bursar(pg_database_url, pg_store)
    try:
        assert bursar.commerce is not None
        bursar.ingest_billing_event(
            BillingEvent(
                provider="stripe",
                event_id="evt_customer_created",
                event_type=BillingEventType.customer_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="mock-customer-1", email="buyer@example.com"),
            )
        )
        bursar.ingest_billing_event(
            BillingEvent(
                provider="stripe",
                event_id="evt_subscription_active",
                event_type=BillingEventType.subscription_created,
                occurred_at=_now(),
                account_id=USER_ID,
                customer=BillingCustomerInfo(provider_customer_id="mock-customer-1"),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id="mock-subscription-1",
                    status=BillingSubscriptionStatus.active,
                    refs=ProviderRef(price_id="price_pro_month"),
                    interval="month",
                    interval_count=1,
                ),
            )
        )

        assert bursar.credits.get_user_plan(USER_ID).plan_key == "pro"
        portal = await bursar.commerce.create_portal_session(USER_ID, "https://app.example/billing")
        assert portal.url == "https://app.example/billing"

        preview = await bursar.commerce.preview_plan_change(USER_ID, offer_key="starter_month")
        assert preview.scheduled is True
        assert preview.quote_fingerprint is not None
        confirmed = await bursar.commerce.confirm_plan_change(
            USER_ID,
            "downgrade-1",
            offer_key="starter_month",
            quote_fingerprint=preview.quote_fingerprint,
        )
        assert confirmed.scheduled is True
        assert confirmed.effective_at == "2026-09-01T00:00:00+00:00"

        assert await bursar.commerce.cancel_scheduled_plan_change(USER_ID, "cancel-downgrade") == {"success": True}
        canceled = await bursar.commerce.cancel_subscription(USER_ID, "cancel-subscription")
        assert canceled.pending is True
    finally:
        billing_store.close()


@pytest.mark.asyncio
async def test_commerce_auto_recharge_processes_saved_payment_attempts(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, provider = _bursar(pg_database_url, pg_store)
    try:
        assert bursar.commerce is not None
        for user_id, customer_id, email in (
            (USER_ID, CUSTOMER_ID, "buyer@example.com"),
            (USER_ID2, CUSTOMER_ID2, "buyer2@example.com"),
            (USER_ID3, CUSTOMER_ID3, "buyer3@example.com"),
        ):
            bursar.ingest_billing_event(
                BillingEvent(
                    provider="stripe",
                    event_id=f"evt_auto_customer_{customer_id}",
                    event_type=BillingEventType.customer_created,
                    occurred_at=_now(),
                    account_id=user_id,
                    customer=BillingCustomerInfo(provider_customer_id=customer_id, email=email),
                )
            )

        enabled = await bursar.commerce.auto_recharge.enable(
            AutoRechargeInput(
                account_id=USER_ID,
                return_url="https://app.example/auto-recharge",
            )
        )
        assert enabled is not None
        assert enabled.enabled is True
        assert enabled.payment_method_last4 == "4242"
        assert enabled.quote_amount_minor == 500
        assert billing_store.count_auto_recharge_attempts(USER_ID, "2000-01-01T00:00:00+00:00") == 1

        blocked_by_cooldown = await bursar.commerce.auto_recharge.process_if_needed(
            AutoRechargeInput(
                account_id=USER_ID,
                return_url="https://app.example/auto-recharge/action",
            )
        )
        assert blocked_by_cooldown.outcome == "limit_reached"

        provider.charges[:] = [
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_action",
                status="requires_customer_action",
                action_url="https://app.example/confirm",
                amount_minor=500,
                currency="USD",
            )
        ]
        action_required = await bursar.commerce.auto_recharge.enable(
            AutoRechargeInput(
                account_id=USER_ID2,
                return_url="https://app.example/auto-recharge/action",
            )
        )
        assert action_required is not None
        assert action_required.state == "paused"
        assert action_required.suspended_reason == "auto_recharge_paused"
        profile = billing_store.get_auto_recharge_profile(USER_ID2)
        assert profile is not None
        assert profile.state == "paused"

        billing_store.update_auto_recharge_attempt_by_provider_payment(
            AutoRechargeProviderPaymentUpdate(
                provider="stripe",
                provider_payment_id="auto_pay_action",
                state="succeeded",
            )
        )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = %s::uuid",
                (USER_ID2,),
            )
            assert cursor.fetchone() == ("succeeded",)
        profile = billing_store.get_auto_recharge_profile(USER_ID2)
        assert profile is not None
        assert profile.state == "active"

        provider.charges[:] = [
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_failed",
                status="failed",
                amount_minor=500,
                currency="USD",
            )
        ]
        failed = await bursar.commerce.auto_recharge.enable(
            AutoRechargeInput(
                account_id=USER_ID3,
                return_url="https://app.example/auto-recharge/failed",
            )
        )
        assert failed is not None
        assert failed.state == "active"
        failed_profile = billing_store.get_auto_recharge_profile(USER_ID3)
        assert failed_profile is not None
        assert failed_profile.state == "active"
        assert failed_profile.armed is True
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, failure_code
                FROM bursar.billing_auto_recharge_attempts
                WHERE subject_id = %s::uuid
                """,
                (USER_ID3,),
            )
            assert cursor.fetchone() == ("failed", "payment_failed")

        provider.charges[:] = [
            SavedPaymentChargeResult(
                provider_payment_id="auto_pay_retry",
                status="processing",
                amount_minor=500,
                currency="USD",
            )
        ]
        await bursar.commerce.auto_recharge.enable(
            AutoRechargeInput(
                account_id=USER_ID3,
                return_url="https://app.example/auto-recharge/enable-again",
            )
        )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = %s::uuid",
                (USER_ID3,),
            )
            assert cursor.fetchall() == [("failed",)]

        retried = await bursar.commerce.auto_recharge.retry(
            AutoRechargeInput(
                account_id=USER_ID3,
                return_url="https://app.example/auto-recharge/retry",
            )
        )
        assert retried is not None
        assert retried.state == "active"
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = %s::uuid",
                (USER_ID3,),
            )
            assert {row[0] for row in cursor.fetchall()} == {"failed", "processing"}

        bursar.commerce.auto_recharge.disable(USER_ID)
        profile = billing_store.get_auto_recharge_profile(USER_ID)
        assert profile is not None
        assert profile.enabled is False
        disabled = await bursar.commerce.auto_recharge.process_if_needed(
            AutoRechargeInput(
                account_id=USER_ID,
                return_url="https://app.example/auto-recharge",
            )
        )
        assert disabled.outcome == "disabled"
    finally:
        billing_store.close()
