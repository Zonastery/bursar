"""Hermetic provider-adapter contract tests (no vendor network calls)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from bursar.billing.models import BillingEventResult
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.stripe.provider import StripeProvider
from bursar.providers.types import (
    ChangePlanParams,
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodSetupParams,
    PortalParams,
    PreviewChangePlanParams,
    UpdatePaymentMethodParams,
    WebhookRequest,
)


class Sink:
    def ingest_billing_event(self, _event: Any) -> BillingEventResult:
        return BillingEventResult(handled=True, action="ok")


class DodoClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.checkout_sessions = self
        self.customers = self
        self.subscriptions = self
        self.payments = self
        self.webhooks = self

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", kwargs))
        return {"checkout_url": "https://checkout.test", "session_id": "sess_1", "customer_id": "cus_1"}

    async def customer_portal(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"link": "https://portal.test"}

    async def update(self, subscription_id: str, **kwargs: Any) -> None:
        self.calls.append((subscription_id, kwargs))

    async def retrieve_payment_methods(self, _customer_id: str) -> dict[str, Any]:
        return {
            "items": [
                {
                    "payment_method": "card",
                    "payment_method_id": "pm_1",
                    "card": {
                        "recurring_enabled": True,
                        "last4_digits": "4242",
                        "card_network": "visa",
                        "expiry_month": 1,
                        "expiry_year": 2030,
                    },
                },
                {"payment_method": "paypal", "payment_method_id": "pm_2"},
            ]
        }

    async def change_plan(self, subscription_id: str, **kwargs: Any) -> None:
        self.calls.append((subscription_id, kwargs))

    async def preview_change_plan(self, _subscription_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "immediate_charge": {"summary": {"total_amount": 12, "settlement_amount": 10, "settlement_currency": "USD"}}
        }

    async def retrieve(self, _payment_id: str) -> dict[str, str]:
        return {"payment_link": "https://invoice.test"}

    async def customers_create(self, **_kwargs: Any) -> dict[str, str]:
        return {"customer_id": "cus_2"}


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_dodo_adapter_maps_requests_and_responses() -> None:
    client = DodoClient()
    provider = DodoProvider(lambda: client, {"setup_product_id": "prod_setup"}, Sink())

    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(product_id="prod_1", return_url="https://return", quantity=2, metadata={"plan": "pro"})
        )
    )
    assert checkout == {"url": "https://checkout.test", "providerSessionId": "sess_1"}
    assert client.calls[0] == (
        "create",
        {
            "product_cart": [{"product_id": "prod_1", "quantity": 2}],
            "return_url": "https://return",
            "metadata": {"plan": "pro"},
        },
    )

    updated = run(
        provider.create_update_payment_method_session(
            UpdatePaymentMethodParams(customer_id="cus_1", subscription_id="sub_1", return_url="https://return")
        )
    )
    assert updated == {"url": "https://checkout.test"}
    run(provider.change_plan(ChangePlanParams(provider_subscription_id="sub_1", product_id="prod_2")))
    preview = run(
        provider.preview_change_plan(PreviewChangePlanParams(provider_subscription_id="sub_1", product_id="prod_2"))
    )
    assert preview.total_amount == 12
    assert run(provider.get_invoice_url("pay_1")) == {"url": "https://invoice.test"}
    assert [p.id for p in run(provider.list_payment_methods("cus_1"))] == ["pm_1"]


def test_dodo_webhook_failures_are_classified_without_network() -> None:
    class Broken:
        class webhooks:
            @staticmethod
            async def unwrap(*_args: Any, **_kwargs: Any) -> None:
                raise TimeoutError("timeout")

    result = run(DodoProvider(lambda: Broken(), {"webhook_key": "k"}, Sink()).handle_webhook(WebhookRequest("{}", {})))
    assert result["received"] is False
    assert result["retryable"] is True


def test_stripe_adapter_maps_requests_and_missing_signature_is_non_retryable() -> None:
    calls: list[tuple[str, Any]] = []

    class Checkout:
        async def create_async(self, **kwargs: Any) -> dict[str, str]:
            calls.append(("checkout", kwargs))
            return {"url": "https://checkout.test"}

    class Customers:
        async def create_async(self, **kwargs: Any) -> dict[str, str]:
            calls.append(("customer", kwargs))
            return {"id": "cus_1"}

    fake = SimpleNamespace(
        Customer=Customers(),
        checkout=SimpleNamespace(Session=Checkout()),
        Webhook=SimpleNamespace(construct_event=lambda *_args: None),
    )
    provider = StripeProvider(Sink(), get_stripe=lambda: fake)
    result = run(
        provider.create_checkout_session(
            CheckoutParams(user_id="u1", product_id="price_1", return_url="https://ok", cancel_url="https://cancel")
        )
    )
    assert result == {"url": "https://checkout.test", "customerId": "cus_1"}
    assert calls[0][0] == "customer"
    assert calls[1][1]["line_items"] == [{"price": "price_1", "quantity": 1}]
    assert run(provider.handle_webhook(WebhookRequest("{}", {}))) == {"received": False, "retryable": False}


def test_mock_provider_is_a_complete_deterministic_test_double() -> None:
    provider = MockPaymentProvider(Sink())
    assert run(provider.create_customer(CreateCustomerParams()))["customerId"].startswith("mock_cus_")
    assert run(provider.create_checkout_session(CheckoutParams(return_url="https://return"))) == {
        "url": "https://return"
    }
    assert run(provider.create_customer_portal_session(PortalParams(return_url="https://portal"))) == {
        "url": "https://portal"
    }
    assert run(provider.create_payment_method_setup_session(PaymentMethodSetupParams(return_url="https://setup"))) == {
        "url": "https://setup"
    }
    assert run(provider.get_invoice_url("pay")) == {"url": "https://example.com/invoice"}
