"""DB-backed commerce integration tests for the Python SDK."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg2
import pytest

from bursar.billing.contracts import AutoRechargeProviderPaymentUpdate
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
from bursar.commerce import AutoRechargeInput, CommerceOptions, CreateCheckoutInput
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.types import (
    ChangePlanPreview,
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
