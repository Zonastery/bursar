"""Hermetic provider-adapter contract tests (no vendor network calls)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from bursar.billing.types import BillingEvent, BillingEventResult, BillingEventType, BillingInvoiceInfo
from bursar.providers._shared import call_billing_event_sink
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.stripe.provider import StripeProvider
from bursar.providers.types import (
    ChangePlanParams,
    CheckoutParams,
    CheckoutSessionResult,
    CreateCustomerParams,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
)


class MinimalProvider(PaymentProvider):
    """Custom provider implementing the same two required capabilities as JS."""

    provider = "minimal"

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        return CheckoutSessionResult(url=params.return_url)

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        del req
        return WebhookResult(
            received=True,
            retryable=False,
            provider="minimal",
            event_id=None,
            event_type=None,
        )


class Sink:
    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        del event
        return BillingEventResult(handled=True, action="ok")


def test_billing_sink_retries_a_busy_claim() -> None:
    results = iter(
        [
            BillingEventResult(handled=False, error="claim_busy"),
            BillingEventResult(handled=False, error="claim_busy"),
            BillingEventResult(handled=True, action="duplicate"),
        ]
    )

    class BusySink:
        def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
            del event
            return next(results)

    sink = BusySink()
    result = call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id="evt_busy",
            event_type=BillingEventType.invoice_paid,
            occurred_at="2026-07-29T12:00:00+00:00",
            invoice=BillingInvoiceInfo(
                provider_invoice_id="in_busy",
                status="paid",
                amount_paid_minor=0,
                amount_due_minor=0,
                currency="USD",
            ),
        ),
    )

    assert result.action == "duplicate"


class DodoClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.checkout_sessions = self
        self.customers = self
        self.subscriptions = self
        self.payments = self
        self.webhooks = self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("create", kwargs))
        return SimpleNamespace(
            checkout_url="https://checkout.test",
            session_id="sess_1",
            customer_id="cus_1",
            payment_id=None,
        )

    async def customer_portal(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"link": "https://portal.test"}

    async def update(self, subscription_id: str, **kwargs: Any) -> None:
        self.calls.append((subscription_id, kwargs))

    async def retrieve_payment_methods(self, _customer_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    payment_method="card",
                    payment_method_id="pm_1",
                    recurring_enabled=True,
                    card=SimpleNamespace(
                        last4_digits="4242",
                        card_network="visa",
                        expiry_month="1",
                        expiry_year="2030",
                    ),
                ),
                SimpleNamespace(
                    payment_method="paypal",
                    payment_method_id="pm_2",
                    recurring_enabled=False,
                    card=None,
                ),
            ]
        )

    async def change_plan(self, subscription_id: str, **kwargs: Any) -> None:
        self.calls.append((subscription_id, kwargs))

    async def preview_change_plan(self, _subscription_id: str, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            immediate_charge=SimpleNamespace(
                effective_at=datetime(2026, 8, 7, tzinfo=UTC),
                line_items=[],
                summary=SimpleNamespace(
                    total_amount=12,
                    settlement_amount=10,
                    settlement_currency="USD",
                    settlement_tax=None,
                    tax=None,
                    customer_credits=0,
                ),
            ),
            new_plan=SimpleNamespace(
                recurring_pre_tax_amount=12,
                currency="USD",
                next_billing_date=datetime(2026, 9, 7, tzinfo=UTC),
            ),
        )

    async def retrieve(self, payment_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            payment_id=payment_id,
            payment_link="https://invoice.test",
            status="succeeded",
            total_amount=12,
            currency="USD",
        )

    async def customers_create(self, **_kwargs: Any) -> dict[str, str]:
        return {"customer_id": "cus_2"}


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_custom_provider_only_requires_the_js_core_contract() -> None:
    provider = MinimalProvider()

    with pytest.raises(NotImplementedError, match="cancel_subscription"):
        run(provider.cancel_subscription("sub_1"))


def test_dodo_adapter_maps_requests_and_responses() -> None:
    client = DodoClient()
    provider = DodoProvider(
        get_client=lambda: client,
        webhook_key="test_webhook_key",
        event_sink=Sink(),
        setup_product_id="prod_setup",
    )

    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(
                product_id="prod_1",
                type="subscription",
                return_url="https://return",
                cancel_url="",
                quantity=2,
                metadata={"plan": "pro"},
            )
        )
    )
    assert checkout.url == "https://checkout.test"
    assert checkout.provider_session_id == "sess_1"
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
    assert updated.url == "https://checkout.test"
    run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_1",
                product_id="prod_2",
                proration_billing_mode="prorated_immediately",
            )
        )
    )
    preview = run(
        provider.preview_change_plan(
            PreviewChangePlanParams(
                provider_subscription_id="sub_1",
                product_id="prod_2",
                proration_billing_mode="prorated_immediately",
            )
        )
    )
    assert preview.total_amount == 12
    invoice = run(provider.get_invoice_url("pay_1"))
    assert invoice is not None
    assert invoice.url == "https://invoice.test"
    assert [p.id for p in run(provider.list_payment_methods("cus_1"))] == ["pm_1"]


def test_dodo_webhook_failures_are_classified_without_network() -> None:
    class Broken:
        class webhooks:
            @staticmethod
            def unwrap(*_args: Any, **_kwargs: Any) -> None:
                raise TimeoutError("timeout")

    result = run(
        DodoProvider(get_client=lambda: Broken(), webhook_key="k", event_sink=Sink()).handle_webhook(
            WebhookRequest(raw_body="{}", headers={})
        )
    )
    assert result.received is False
    assert result.retryable is False
    assert result.provider == "dodo"


def test_stripe_adapter_maps_requests_and_missing_signature_is_non_retryable() -> None:
    calls: list[tuple[str, Any]] = []

    class Checkout:
        async def create_async(self, **kwargs: Any) -> dict[str, str]:
            calls.append(("checkout", kwargs))
            return {"id": "cs_1", "url": "https://checkout.test"}

    class Customers:
        async def create_async(self, **kwargs: Any) -> dict[str, str]:
            calls.append(("customer", kwargs))
            return {"id": "cus_1"}

    fake = SimpleNamespace(
        Customer=Customers(),
        checkout=SimpleNamespace(Session=Checkout()),
        Webhook=SimpleNamespace(construct_event=lambda *_args: None),
    )
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: fake,
    )
    result = run(
        provider.create_checkout_session(
            CheckoutParams(
                user_id="u1",
                product_id="price_1",
                type="subscription",
                return_url="https://ok",
                cancel_url="https://cancel",
                metadata={},
            )
        )
    )
    assert result.url == "https://checkout.test"
    assert result.customer_id == "cus_1"
    assert calls[0][0] == "customer"
    assert calls[1][1]["line_items"] == [{"price": "price_1", "quantity": 1}]
    webhook = run(provider.handle_webhook(WebhookRequest(raw_body="{}", headers={})))
    assert webhook.received is False
    assert webhook.retryable is False
    assert webhook.provider == "stripe"


def test_mock_provider_is_a_complete_deterministic_test_double() -> None:
    provider = MockPaymentProvider(event_sink=Sink())
    customer = run(provider.create_customer(CreateCustomerParams(email="", name="", metadata={})))
    assert customer.customer_id.startswith("mock_cus_")
    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(
                product_id="mock",
                type="subscription",
                return_url="https://return",
                cancel_url="",
                metadata={},
            )
        )
    )
    assert checkout.url == "https://return"
    portal = run(
        provider.create_customer_portal_session(PortalParams(customer_id="mock_customer", return_url="https://portal"))
    )
    assert portal.url == "https://portal"
    setup = run(
        provider.create_payment_method_setup_session(
            PaymentMethodSetupParams(customer_id="mock_customer", return_url="https://setup")
        )
    )
    assert setup.url == "https://setup"
    invoice = run(provider.get_invoice_url("pay"))
    assert invoice is not None
    assert invoice.url == "https://example.com/invoice"
