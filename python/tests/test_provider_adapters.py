"""Hermetic provider-adapter contract tests (no vendor network calls)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import stripe
from dodopayments import AsyncDodoPayments
from stripe import StripeClient, StripeObject
from stripe._http_client import HTTPClient

from bursar.billing.types import BillingEvent, BillingEventResult, BillingEventType, BillingInvoiceInfo
from bursar.errors import StoreUnavailableError
from bursar.providers._shared import call_billing_event_sink
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.stripe.provider import StripeProvider, _scoped_idempotency_key, _stripe_dict
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    CheckoutParams,
    CheckoutSessionResult,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    SavedPaymentChargeParams,
    SubscriptionCancellationProvider,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
)
from bursar.shared.idempotency import scope_stable_key


class MinimalProvider:
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


def test_stripe_dict_preserves_mappings_and_fails_closed_for_unmapped_objects() -> None:
    assert _stripe_dict({"metadata": {"account": "u1"}}) == {"metadata": {"account": "u1"}}
    stripe_object = StripeObject.construct_from({"metadata": {"account": "u1"}}, "sk_test")
    assert _stripe_dict(stripe_object) == {"metadata": {"account": "u1"}}

    class RecursiveMapping:
        def to_dict_recursive(self) -> dict[str, dict[str, str]]:
            return {"metadata": {"account": "u1"}}

    assert _stripe_dict(RecursiveMapping()) == {"metadata": {"account": "u1"}}

    class UnmappedObject:
        metadata: ClassVar[dict[str, str]] = {"account": "should-not-be-read"}

    assert _stripe_dict(UnmappedObject()) == {}


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


@pytest.mark.parametrize(
    "error",
    ["invalid_request", "idempotency_conflict", "max_retries_exceeded"],
)
def test_billing_sink_acknowledges_permanent_claim_outcomes(error: str) -> None:
    class PermanentSink:
        def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
            del event
            return BillingEventResult(handled=False, error=error)

    result = call_billing_event_sink(
        PermanentSink(),
        BillingEvent(
            provider="stripe",
            event_id=f"evt_{error}",
            event_type=BillingEventType.invoice_paid,
            occurred_at="2026-07-29T12:00:00+00:00",
            invoice=BillingInvoiceInfo(
                provider_invoice_id=f"in_{error}",
                status="paid",
                amount_paid_minor=0,
                amount_due_minor=0,
                currency="USD",
            ),
        ),
    )

    assert result.error == error


def test_billing_sink_keeps_legacy_retry_claim_retryable() -> None:
    class RetrySink:
        def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
            del event
            return BillingEventResult(handled=False, error="claim_failed_retry")

    with pytest.raises(StoreUnavailableError):
        call_billing_event_sink(
            RetrySink(),
            BillingEvent(
                provider="stripe",
                event_id="evt_claim_retry",
                event_type=BillingEventType.invoice_paid,
                occurred_at="2026-07-29T12:00:00+00:00",
                invoice=BillingInvoiceInfo(
                    provider_invoice_id="in_claim_retry",
                    status="paid",
                    amount_paid_minor=0,
                    amount_due_minor=0,
                    currency="USD",
                ),
            ),
        )


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

    async def update_payment_method(self, subscription_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append((subscription_id, kwargs))
        return SimpleNamespace(payment_link="https://update-payment-method.test")

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
                line_items=[
                    SimpleNamespace(
                        type="subscription",
                        product_id="prod_2",
                        name=None,
                        description="Prorated Pro",
                        unit_price=100,
                        quantity=2,
                        proration_factor=0.75,
                        currency="USD",
                        tax=3,
                    )
                ],
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
            payment_link="https://checkout.test/not-an-invoice",
            invoice_url="https://invoice.test/document.pdf",
            status="succeeded",
            total_amount=12,
            currency="USD",
        )

    async def customers_create(self, **_kwargs: Any) -> dict[str, str]:
        return {"customer_id": "cus_2"}


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class StripeMockTransport(HTTPClient):
    name = "transport"

    def __init__(self, *, missing_checkout: bool = False) -> None:
        super().__init__(verify_ssl_certs=False)
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.request_headers: list[dict[str, str]] = []
        self.missing_checkout = missing_checkout

    async def request_async(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        post_data: bytes | None = None,
    ) -> tuple[bytes, int, dict[str, str]]:
        parsed = urlsplit(url)
        encoded_data = post_data.decode() if isinstance(post_data, bytes) else post_data
        form = parse_qs(encoded_data or "")
        self.requests.append((method.upper(), parsed.path, form))
        self.request_headers.append(dict(headers))
        response: dict[str, Any]
        if parsed.path == "/v1/checkout/sessions/cs_missing" and self.missing_checkout:
            response = {
                "error": {
                    "type": "invalid_request_error",
                    "code": "resource_missing",
                    "message": "No such checkout session",
                }
            }
            return json.dumps(response).encode(), 404, {"content-type": "application/json"}
        if parsed.path == "/v1/customers":
            response = {"id": "cus_transport", "object": "customer"}
        elif parsed.path == "/v1/checkout/sessions":
            response = {"id": "cs_transport", "url": "https://checkout.transport"}
        elif parsed.path == "/v1/billing_portal/sessions":
            response = {"id": "bps_transport", "url": "https://portal.transport"}
        elif parsed.path == "/v1/customers/cus_transport":
            response = {
                "id": "cus_transport",
                "invoice_settings": {"default_payment_method": "pm_transport"},
            }
        elif parsed.path in {
            "/v1/payment_methods",
            "/v1/customers/cus_transport/payment_methods",
        }:
            response = {
                "data": [
                    {
                        "id": "pm_transport",
                        "card": {"last4": "4242", "brand": "visa", "exp_month": 12, "exp_year": 2030},
                    }
                ]
            }
        elif parsed.path == "/v1/prices/price_transport":
            response = {"id": "price_transport", "unit_amount": 500, "currency": "usd"}
        elif parsed.path == "/v1/payment_intents":
            response = {
                "id": "pi_transport",
                "status": "requires_action",
                "amount": 500,
                "currency": "usd",
            }
        elif parsed.path == "/v1/invoices/in_transport":
            response = {"id": "in_transport", "hosted_invoice_url": "https://invoice.transport"}
        elif parsed.path == "/v1/subscriptions/sub_transport":
            response = {
                "id": "sub_transport",
                "items": {"data": [{"id": "si_transport"}]},
                "latest_invoice": "in_transport",
            }
        else:
            raise AssertionError(f"unexpected Stripe request: {method} {parsed.path}")
        return json.dumps(response).encode(), 200, {"content-type": "application/json"}


def test_custom_provider_only_requires_the_js_core_contract() -> None:
    provider = MinimalProvider()

    assert isinstance(provider, PaymentProvider)
    assert not isinstance(provider, SubscriptionCancellationProvider)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: CheckoutParams(
                account_id="user-1",
                product_id="product-1",
                type="credit_pack",
                quantity=0,
                return_url="https://return.test",
                cancel_url="https://cancel.test",
                metadata={},
                idempotency_key="invalid-quantity",
            ),
            "quantity",
        ),
        (
            lambda: CheckoutParams(
                account_id="user-1",
                product_id="product-1",
                type="credit_pack",
                quantity=2**53,
                return_url="https://return.test",
                cancel_url="https://cancel.test",
                metadata={},
                idempotency_key="unsafe-quantity",
            ),
            "quantity",
        ),
        (
            lambda: PaymentMethodInfo(
                id="payment-method-1",
                last4="42",
                brand="visa",
                expiry_month=12,
                expiry_year=2030,
            ),
            "last4",
        ),
    ],
)
def test_provider_contracts_reject_malformed_payment_data(factory: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()


def test_provider_mutation_keys_are_not_silently_trimmed() -> None:
    with pytest.raises(ValueError, match="trimmed non-empty string"):
        CheckoutParams(
            account_id="user-1",
            product_id="product-1",
            type="credit_pack",
            return_url="https://return.test",
            cancel_url="https://cancel.test",
            metadata={},
            idempotency_key=" padded-key ",
        )


def test_stripe_scoped_keys_hash_overlong_candidates_without_prefix_collisions() -> None:
    first = _scoped_idempotency_key(f"{'x' * 254}a", "customer")
    second = _scoped_idempotency_key(f"{'x' * 254}b", "customer")

    assert first.startswith("bursar:customer:")
    assert len(first) <= 255
    assert first != second
    assert _scoped_idempotency_key("checkout-1", "customer") == "checkout-1:customer"


def test_scoped_keys_encode_dynamic_identity_boundaries() -> None:
    first = scope_stable_key("operation", "cancel-all", "a", "b:c")
    second = scope_stable_key("operation", "cancel-all", "a:b", "c")
    hashed_first = scope_stable_key("x" * 255, "cancel-all", "a", "b:c")
    hashed_second = scope_stable_key("x" * 255, "cancel-all", "a:b", "c")

    assert first == "operation:cancel-all:1#a:3#b:c"
    assert second == "operation:cancel-all:3#a:b:1#c"
    assert first != second
    assert hashed_first.startswith("bursar:cancel-all:")
    assert hashed_second.startswith("bursar:cancel-all:")
    assert hashed_first != hashed_second


def test_plan_preview_contract_normalizes_currency_and_allows_signed_credits() -> None:
    preview = ChangePlanPreview(
        total_amount=-25,
        settlement_amount=-25,
        currency="usd",
        line_items=[
            ChangePlanLineItem(
                product_id="product-1",
                name="Proration credit",
                unit_price=-25,
                quantity=1,
                proration_factor=1.0,
                currency="usd",
                tax=-2,
                subtotal=-25,
            )
        ],
        effective_at="2026-08-01T00:00:00Z",
    )

    assert preview.currency == "USD"
    assert preview.line_items[0].currency == "USD"
    assert preview.line_items[0].tax == -2


def test_dodo_adapter_maps_requests_and_responses() -> None:
    client = DodoClient()
    provider = DodoProvider(
        get_client=lambda: cast(AsyncDodoPayments, client),
        webhook_key="test_webhook_key",
        event_sink=Sink(),
        setup_product_id="prod_setup",
    )

    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(
                account_id="user-1",
                product_id="prod_1",
                type="subscription",
                return_url="https://return",
                cancel_url="",
                quantity=2,
                metadata={"plan": "pro", "bursar_account_id": "spoofed"},
                idempotency_key="checkout-1",
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
            "metadata": {"plan": "pro", "bursar_account_id": "user-1"},
            "extra_headers": {"Idempotency-Key": "checkout-1"},
        },
    )

    updated = run(
        provider.create_update_payment_method_session(
            UpdatePaymentMethodParams(customer_id="cus_1", subscription_id="sub_1", return_url="https://return")
        )
    )
    assert updated.url == "https://update-payment-method.test"
    assert client.calls[1] == (
        "sub_1",
        {"payment_method": {"type": "new", "return_url": "https://return"}},
    )
    setup = run(
        provider.create_payment_method_setup_session(
            PaymentMethodSetupParams(customer_id="cus_1", return_url="https://return/setup")
        )
    )
    assert setup.url == "https://checkout.test"
    assert client.calls[2] == (
        "create",
        {
            "product_cart": [{"product_id": "prod_setup", "quantity": 1}],
            "customer": {"customer_id": "cus_1"},
            "return_url": "https://return/setup",
            "metadata": {"purpose": "setup_payment_method"},
            "subscription_data": {"on_demand": {"mandate_only": True}},
        },
    )
    run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_1",
                product_id="prod_2",
                proration_billing_mode="prorated_immediately",
                idempotency_key="change-plan-1",
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
    assert preview.line_items == [
        ChangePlanLineItem(
            product_id="prod_2",
            name="Prorated Pro",
            unit_price=100,
            quantity=2,
            proration_factor=0.75,
            currency="USD",
            tax=3,
            subtotal=150,
        )
    ]
    customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="test@example.com",
                name="Test User",
                metadata={"source": "test"},
                idempotency_key="dodo-customer-1",
            )
        )
    )
    assert customer.customer_id == "cus_1"
    assert client.calls[-1][1]["extra_headers"] == {"Idempotency-Key": "dodo-customer-1"}
    invoice = run(provider.get_invoice_url("pay_1"))
    assert invoice is not None
    assert invoice.url == "https://invoice.test/document.pdf"
    assert [p.id for p in run(provider.list_payment_methods("cus_1"))] == ["pm_1"]


def test_dodo_http_transport_covers_checkout_portal_payment_and_plan_operations() -> None:
    requests: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append((request.method, request.url.path, body, dict(request.headers)))
        if request.url.path == "/checkouts":
            return httpx.Response(
                200,
                json={
                    "session_id": "sess_transport",
                    "checkout_url": "https://checkout.dodo.transport",
                    "payment_id": "pay_transport",
                },
            )
        if request.url.path == "/customers/cus_transport/customer-portal/session":
            return httpx.Response(200, json={"link": "https://portal.dodo.transport"})
        if request.url.path == "/subscriptions/sub_transport/update-payment-method":
            return httpx.Response(200, json={"payment_link": "https://payment.dodo.transport"})
        if request.url.path == "/customers/cus_transport/payment-methods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "payment_method": "card",
                            "payment_method_id": "pm_dodo_transport",
                            "recurring_enabled": True,
                            "card": {
                                "last4_digits": "4242",
                                "card_network": "visa",
                                "expiry_month": 12,
                                "expiry_year": 2030,
                            },
                        },
                        {
                            "payment_method": "paypal",
                            "payment_method_id": "pm_paypal",
                            "recurring_enabled": False,
                            "card": None,
                        },
                    ]
                },
            )
        if request.url.path == "/payments/pay_transport":
            return httpx.Response(
                200,
                json={
                    "billing": {"country": "US"},
                    "brand_id": "brand_transport",
                    "business_id": "business_transport",
                    "created_at": "2026-08-19T10:00:00Z",
                    "currency": "USD",
                    "customer": {
                        "customer_id": "cus_transport",
                        "email": "transport@example.com",
                        "name": "Transport User",
                    },
                    "digital_products_delivered": False,
                    "disputes": [],
                    "is_update_payment_method": False,
                    "metadata": {},
                    "payment_id": "pay_transport",
                    "payment_provider": "dodo",
                    "refunds": [],
                    "retry_attempt": 0,
                    "settlement_amount": 500,
                    "settlement_currency": "USD",
                    "total_amount": 500,
                    "status": "succeeded",
                },
            )
        if request.url.path == "/subscriptions/sub_transport/change-plan":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected Dodo request: {request.method} {request.url.path}")

    async def exercise() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.dodo.transport",
        )
        client = AsyncDodoPayments(
            bearer_token="test-token",
            base_url="https://api.dodo.transport",
            http_client=http_client,
        )
        provider = DodoProvider(
            get_client=lambda: client,
            webhook_key="test_webhook_key",
            event_sink=Sink(),
        )
        checkout = await provider.create_checkout_session(
            CheckoutParams(
                account_id="transport-user",
                customer_id="cus_transport",
                product_id="prod_transport",
                type="credit_pack",
                quantity=2,
                return_url="https://return.transport",
                cancel_url="https://cancel.transport",
                metadata={},
                idempotency_key="dodo-transport-checkout",
            )
        )
        assert checkout.provider_session_id == "sess_transport"
        assert (
            await provider.create_customer_portal_session(
                PortalParams(customer_id="cus_transport", return_url="https://portal")
            )
        ).url == "https://portal.dodo.transport"
        assert (
            await provider.create_update_payment_method_session(
                UpdatePaymentMethodParams(
                    customer_id="cus_transport",
                    subscription_id="sub_transport",
                    return_url="https://payment-method",
                )
            )
        ).url == "https://payment.dodo.transport"
        methods = await provider.list_payment_methods("cus_transport")
        assert methods == [
            PaymentMethodInfo(
                id="pm_dodo_transport",
                last4="4242",
                brand="visa",
                expiry_month=12,
                expiry_year=2030,
                is_default=True,
            )
        ]
        charged = await provider.charge_saved_payment_method(
            SavedPaymentChargeParams(
                customer_id="cus_transport",
                payment_method_id="pm_dodo_transport",
                product_id="prod_transport",
                quantity=1,
                metadata={},
                idempotency_key="dodo-transport-charge",
                return_url="https://return.transport",
            )
        )
        assert charged.provider_payment_id == "pay_transport"
        assert charged.amount_minor == 500
        await provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_transport",
                product_id="prod_transport",
                proration_billing_mode="prorated_immediately",
                effective_at="immediately",
                on_payment_failure="prevent_change",
                idempotency_key="dodo-transport-plan",
            )
        )
        await client.close()

    run(exercise())
    assert [(method, path) for method, path, _body, _headers in requests] == [
        ("POST", "/checkouts"),
        ("POST", "/customers/cus_transport/customer-portal/session"),
        ("POST", "/subscriptions/sub_transport/update-payment-method"),
        ("GET", "/customers/cus_transport/payment-methods"),
        ("POST", "/checkouts"),
        ("GET", "/payments/pay_transport"),
        ("POST", "/subscriptions/sub_transport/change-plan"),
    ]
    assert requests[0][2]["customer"] == {"customer_id": "cus_transport"}
    assert requests[0][2]["product_cart"] == [{"product_id": "prod_transport", "quantity": 2}]
    assert requests[0][3]["idempotency-key"] == "dodo-transport-checkout"
    assert requests[4][2]["confirm"] is True
    assert requests[4][3]["idempotency-key"] == "dodo-transport-charge"
    assert requests[6][2]["product_id"] == "prod_transport"
    assert requests[6][3]["idempotency-key"] == "dodo-transport-plan"


def test_dodo_http_404_checkout_missing_maps_to_empty_checkout_status() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(404, json={"message": "checkout not found"})

    async def exercise() -> None:
        client = AsyncDodoPayments(
            bearer_token="test-token",
            base_url="https://api.dodo.transport",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://api.dodo.transport",
            ),
        )
        provider = DodoProvider(
            get_client=lambda: client,
            webhook_key="test_webhook_key",
            event_sink=Sink(),
        )
        assert await provider.get_checkout_session_status("dodo_missing") is None
        await client.close()

    run(exercise())
    assert requests == [("GET", "/checkouts/dodo_missing")]


def test_dodo_webhook_failures_are_classified_without_network() -> None:
    class Broken:
        class webhooks:
            @staticmethod
            def unwrap(*_args: Any, **_kwargs: Any) -> None:
                raise TimeoutError("timeout")

    result = run(
        DodoProvider(
            get_client=lambda: cast(AsyncDodoPayments, Broken()),
            webhook_key="k",
            event_sink=Sink(),
        ).handle_webhook(WebhookRequest(raw_body="{}", headers={}))
    )
    assert result.received is False
    assert result.retryable is False
    assert result.provider == "dodo"


def test_stripe_adapter_maps_requests_and_missing_signature_is_non_retryable() -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    class Checkout:
        async def create_async(
            self,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            calls.append(("checkout", params, options))
            return SimpleNamespace(id="cs_1", url="https://checkout.test")

    class Customers:
        async def create_async(
            self,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            calls.append(("customer", params, options))
            return SimpleNamespace(id="cus_1")

    fake = SimpleNamespace(
        v1=SimpleNamespace(
            customers=Customers(),
            checkout=SimpleNamespace(sessions=Checkout()),
        ),
        construct_event=lambda *_args: None,
    )
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: cast(StripeClient, fake),
    )
    result = run(
        provider.create_checkout_session(
            CheckoutParams(
                account_id="u1",
                product_id="price_1",
                type="subscription",
                return_url="https://ok",
                cancel_url="https://cancel",
                metadata={},
                email="u1@example.com",
                idempotency_key="idem_1",
            )
        )
    )
    assert result.url == "https://checkout.test"
    assert result.customer_id == "cus_1"
    assert result.provider_session_id == "cs_1"
    assert calls[0][0] == "customer"
    assert calls[0][1]["email"] == "u1@example.com"
    assert calls[0][1]["metadata"] == {"bursar_account_id": "u1"}
    assert calls[0][2] == {"idempotency_key": "idem_1:customer"}
    assert calls[1][1]["line_items"] == [{"price": "price_1", "quantity": 1}]
    assert calls[1][1]["metadata"]["bursar_account_id"] == "u1"
    assert calls[1][1]["subscription_data"]["metadata"]["bursar_account_id"] == "u1"
    assert calls[1][2] == {"idempotency_key": "idem_1"}
    customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="other@example.com",
                name="Other User",
                metadata={},
                idempotency_key="stripe-customer-1",
            )
        )
    )
    assert customer.customer_id == "cus_1"
    assert calls[-1][2] == {"idempotency_key": "stripe-customer-1"}
    webhook = run(provider.handle_webhook(WebhookRequest(raw_body="{}", headers={})))
    assert webhook.received is False
    assert webhook.retryable is False
    assert webhook.provider == "stripe"


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (stripe.SignatureVerificationError("invalid signature", "bad-signature"), False),
        (stripe.APIError("provider unavailable"), True),
        (RuntimeError("malformed provider response"), False),
    ],
)
def test_stripe_webhook_verification_failures_are_classified(error: Exception, retryable: bool) -> None:
    class BrokenStripe:
        def construct_event(self, *_args: Any) -> None:
            raise error

    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: cast(StripeClient, BrokenStripe()),
    )

    result = run(
        provider.handle_webhook(
            WebhookRequest(
                raw_body="{}",
                headers={"stripe-signature": "bad-signature"},
            )
        )
    )

    assert result.received is False
    assert result.retryable is retryable
    assert result.event_id is None


def test_stripe_http_transport_covers_customer_portal_payment_and_plan_operations() -> None:
    transport = StripeMockTransport()
    client = StripeClient("sk_test_transport", http_client=transport)
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: client,
    )

    customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="transport@example.com",
                name="Transport User",
                metadata={"source": "transport"},
                idempotency_key="transport-customer",
            )
        )
    )
    assert customer.customer_id == "cus_transport"
    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(
                account_id="transport-user",
                customer_id=customer.customer_id,
                product_id="price_transport",
                type="credit_pack",
                quantity=2,
                return_url="https://return.transport",
                cancel_url="https://cancel.transport",
                metadata={},
                idempotency_key="transport-checkout",
            )
        )
    )
    assert checkout.provider_session_id == "cs_transport"
    portal = run(
        provider.create_customer_portal_session(
            PortalParams(customer_id=customer.customer_id, return_url="https://portal")
        )
    )
    assert portal.url == "https://portal.transport"
    assert (
        run(
            provider.create_update_payment_method_session(
                UpdatePaymentMethodParams(
                    customer_id=customer.customer_id,
                    subscription_id="sub_transport",
                    return_url="https://payment-method",
                )
            )
        ).url
        == "https://portal.transport"
    )
    assert (
        run(
            provider.create_payment_method_setup_session(
                PaymentMethodSetupParams(customer_id=customer.customer_id, return_url="https://setup")
            )
        ).url
        == "https://checkout.transport"
    )
    methods = run(provider.list_payment_methods(customer.customer_id))
    assert methods[0].is_default is True
    assert methods[0].last4 == "4242"
    assert (
        run(
            provider.preview_saved_payment_charge(
                SavedPaymentChargeParams(
                    customer_id=customer.customer_id,
                    payment_method_id="pm_transport",
                    product_id="price_transport",
                    quantity=2,
                    metadata={},
                    idempotency_key="transport-preview",
                )
            )
        ).amount_minor
        == 1000
    )
    charged = run(
        provider.charge_saved_payment_method(
            SavedPaymentChargeParams(
                customer_id=customer.customer_id,
                payment_method_id="pm_transport",
                product_id="price_transport",
                quantity=1,
                metadata={},
                idempotency_key="transport-charge",
            )
        )
    )
    assert charged.status == "requires_customer_action"
    assert run(provider.get_invoice_url("in_transport")).url == "https://invoice.transport"
    changed = run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_transport",
                product_id="price_transport",
                proration_billing_mode="prorated_immediately",
                effective_at="immediately",
                on_payment_failure="prevent_change",
                metadata={"plan": "pro"},
                idempotency_key="transport-plan",
            )
        )
    )
    assert changed.provider_operation_id == "in_transport"
    run(provider.cancel_subscription("sub_transport", "transport-cancel"))
    run(provider.reactivate_subscription("sub_transport", "transport-reactivate"))
    assert [(method, path) for method, path, _form in transport.requests] == [
        ("POST", "/v1/customers"),
        ("POST", "/v1/checkout/sessions"),
        ("POST", "/v1/billing_portal/sessions"),
        ("POST", "/v1/billing_portal/sessions"),
        ("POST", "/v1/checkout/sessions"),
        ("GET", "/v1/customers/cus_transport"),
        ("GET", "/v1/customers/cus_transport/payment_methods"),
        ("GET", "/v1/prices/price_transport"),
        ("GET", "/v1/prices/price_transport"),
        ("POST", "/v1/payment_intents"),
        ("GET", "/v1/invoices/in_transport"),
        ("GET", "/v1/subscriptions/sub_transport"),
        ("POST", "/v1/subscriptions/sub_transport"),
        ("POST", "/v1/subscriptions/sub_transport"),
        ("POST", "/v1/subscriptions/sub_transport"),
    ]
    assert transport.request_headers[0]["Idempotency-Key"] == "transport-customer"
    assert transport.request_headers[1]["Idempotency-Key"] == "transport-checkout"
    assert transport.requests[1][2]["customer"] == ["cus_transport"]
    assert transport.requests[1][2]["line_items[0][price]"] == ["price_transport"]
    assert transport.request_headers[12]["Idempotency-Key"] == "transport-plan:subscription-update"
    assert transport.request_headers[13]["Idempotency-Key"] == "transport-cancel"
    assert transport.request_headers[14]["Idempotency-Key"] == "transport-reactivate"


def test_stripe_http_404_resource_missing_maps_to_empty_checkout_status() -> None:
    transport = StripeMockTransport(missing_checkout=True)
    client = StripeClient("sk_test_transport", http_client=transport)
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: client,
    )

    assert run(provider.get_checkout_session_status("cs_missing")) is None
    assert transport.requests == [("GET", "/v1/checkout/sessions/cs_missing", {})]


def test_stripe_uses_current_checkout_status_and_subscription_schedule_apis() -> None:
    subscription_updates: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []
    schedule_creates: list[tuple[dict[str, Any], dict[str, str] | None]] = []
    schedule_updates: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []
    schedule_releases: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    class Checkout:
        async def retrieve_async(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
            assert params == {"expand": ["payment_intent"]}
            if session_id == "cs_open":
                return {"status": "open", "payment_status": "unpaid"}
            return {
                "status": "complete",
                "payment_status": "unpaid",
                "payment_intent": {"status": "requires_payment_method"},
            }

    class Subscription:
        async def retrieve_async(self, _subscription_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                customer="cus_1",
                items=SimpleNamespace(
                    data=[
                        SimpleNamespace(
                            id="si_1",
                            current_period_start=1_767_225_600,
                            current_period_end=1_769_904_000,
                        )
                    ]
                ),
            )

        async def update_async(
            self,
            subscription_id: str,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            subscription_updates.append((subscription_id, params, options))
            return SimpleNamespace(latest_invoice="in_1")

    class SubscriptionSchedule:
        async def create_async(
            self,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            schedule_creates.append((params, options))
            return SimpleNamespace(
                id="sub_sched_1",
                phases=[
                    SimpleNamespace(
                        items=[SimpleNamespace(price="price_old", quantity=1)],
                        start_date=1_767_225_600,
                        end_date=1_769_904_000,
                    )
                ],
            )

        async def update_async(
            self,
            schedule_id: str,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            schedule_updates.append((schedule_id, params, options))
            return SimpleNamespace()

        async def release_async(
            self,
            schedule_id: str,
            params: dict[str, Any],
            options: dict[str, str] | None = None,
        ) -> SimpleNamespace:
            schedule_releases.append((schedule_id, params, options))
            return SimpleNamespace()

    fake = SimpleNamespace(
        v1=SimpleNamespace(
            checkout=SimpleNamespace(sessions=Checkout()),
            subscriptions=Subscription(),
            subscription_schedules=SubscriptionSchedule(),
        ),
    )
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: cast(StripeClient, fake),
    )

    assert run(provider.get_checkout_session_status("cs_open")).payment_status == "processing"
    assert run(provider.get_checkout_session_status("cs_requires_method")).payment_status == "requires_payment_method"

    scheduled = run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_1",
                product_id="price_new",
                proration_billing_mode="do_not_bill",
                effective_at="next_billing_date",
                metadata={"plan": "pro"},
                idempotency_key="plan_1",
            )
        )
    )
    assert scheduled.provider_operation_id == "sub_sched_1"
    assert schedule_creates == [({"from_subscription": "sub_1"}, {"idempotency_key": "plan_1:schedule-create"})]
    schedule_id, schedule_kwargs, schedule_options = schedule_updates[0]
    assert schedule_id == "sub_sched_1"
    assert schedule_options == {"idempotency_key": "plan_1:schedule-update"}
    assert schedule_kwargs["phases"][0]["items"] == [{"price": "price_old", "quantity": 1}]
    assert schedule_kwargs["phases"][1] == {
        "items": [{"price": "price_new", "quantity": 1}],
        "start_date": 1_769_904_000,
        "proration_behavior": "none",
        "metadata": {"plan": "pro"},
    }

    immediate = run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="sub_1",
                product_id="price_now",
                proration_billing_mode="do_not_bill",
                effective_at="immediately",
                on_payment_failure="apply_change",
                metadata={"plan": "team"},
                idempotency_key="plan_2",
            )
        )
    )
    assert immediate.provider_operation_id == "in_1"
    assert subscription_updates[-1] == (
        "sub_1",
        {
            "items": [{"id": "si_1", "price": "price_now", "quantity": 1}],
            "proration_behavior": "none",
            "payment_behavior": "allow_incomplete",
            "metadata": {"plan": "team"},
        },
        {"idempotency_key": "plan_2:subscription-update"},
    )

    run(provider.cancel_subscription("sub_1", "cancel_1"))
    run(provider.reactivate_subscription("sub_1", "reactivate_1"))
    run(provider.cancel_scheduled_plan_change("sub_1", "sub_sched_1", idempotency_key="release_1"))
    assert subscription_updates[-2:] == [
        ("sub_1", {"cancel_at_period_end": True}, {"idempotency_key": "cancel_1"}),
        ("sub_1", {"cancel_at_period_end": False}, {"idempotency_key": "reactivate_1"}),
    ]
    assert schedule_releases == [("sub_sched_1", {}, {"idempotency_key": "release_1"})]


def test_stripe_plan_preview_preserves_proration_tax_and_recurring_amounts() -> None:
    period_end = 1_769_904_000

    class Subscriptions:
        async def retrieve_async(self, _subscription_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                customer="cus_preview",
                items=SimpleNamespace(data=[SimpleNamespace(id="si_preview", current_period_end=period_end)]),
            )

    class Invoices:
        async def create_preview_async(self, _params: dict[str, Any]) -> SimpleNamespace:
            billable = SimpleNamespace(
                parent=SimpleNamespace(subscription_item_details=SimpleNamespace()),
                pricing=SimpleNamespace(
                    price_details=SimpleNamespace(price="price_pro"),
                    unit_amount_decimal="500",
                ),
                quantity=2,
                subtotal=750,
                taxes=[SimpleNamespace(amount=75)],
                description="Prorated Pro",
                currency="usd",
            )
            return SimpleNamespace(
                total=825,
                amount_due=825,
                currency="usd",
                created=1_767_225_600,
                lines=SimpleNamespace(
                    data=[SimpleNamespace(parent=None), billable],
                ),
                total_taxes=[SimpleNamespace(amount=75)],
            )

    class Prices:
        async def retrieve_async(self, _price_id: str) -> SimpleNamespace:
            return SimpleNamespace(unit_amount=1000, currency="usd")

    fake = SimpleNamespace(
        v1=SimpleNamespace(
            subscriptions=Subscriptions(),
            invoices=Invoices(),
            prices=Prices(),
        )
    )
    provider = StripeProvider(
        event_sink=Sink(),
        webhook_secret="test_webhook_secret",
        get_client=lambda: cast(StripeClient, fake),
    )

    preview = run(
        provider.preview_change_plan(
            PreviewChangePlanParams(
                provider_subscription_id="sub_preview",
                product_id="price_pro",
                quantity=1,
                effective_at="immediately",
                proration_billing_mode="prorated_immediately",
            )
        )
    )

    assert preview.total_amount == 825
    assert preview.settlement_amount == 825
    assert preview.recurring_amount == 1000
    assert preview.next_billing_date == datetime.fromtimestamp(period_end, tz=UTC).isoformat()
    assert preview.tax_amount == 75
    assert preview.line_items == [
        ChangePlanLineItem(
            product_id="price_pro",
            name="Prorated Pro",
            unit_price=500,
            quantity=2,
            proration_factor=0.75,
            currency="usd",
            tax=75,
            subtotal=750,
        )
    ]


def test_mock_provider_is_a_complete_deterministic_test_double() -> None:
    provider = MockPaymentProvider(event_sink=Sink())
    first_customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="test@example.com",
                name="Test User",
                metadata={},
                idempotency_key="customer-1",
            )
        )
    )
    replayed_customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="test@example.com",
                name="Test User",
                metadata={},
                idempotency_key="customer-1",
            )
        )
    )
    second_customer = run(
        provider.create_customer(
            CreateCustomerParams(
                email="other@example.com",
                name="Other User",
                metadata={},
                idempotency_key="customer-2",
            )
        )
    )
    assert [first_customer.customer_id, replayed_customer.customer_id, second_customer.customer_id] == [
        "mock_cus_1",
        "mock_cus_1",
        "mock_cus_2",
    ]
    checkout = run(
        provider.create_checkout_session(
            CheckoutParams(
                account_id="user-1",
                product_id="mock",
                type="subscription",
                return_url="https://return",
                cancel_url="",
                metadata={},
                idempotency_key="mock-checkout-1",
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
    update = run(
        provider.create_update_payment_method_session(
            UpdatePaymentMethodParams(
                customer_id="mock_customer",
                subscription_id="mock_subscription",
                return_url="https://update",
            )
        )
    )
    assert update.url == "https://update"
    assert run(provider.list_payment_methods("mock_customer")) == []
    charge_params = SavedPaymentChargeParams(
        customer_id="mock_customer",
        payment_method_id="mock_payment_method",
        product_id="mock",
        quantity=2,
        metadata={},
        idempotency_key="mock-charge-1",
        return_url="https://return",
    )
    quote = run(provider.preview_saved_payment_charge(charge_params))
    assert quote.amount_minor == 0
    charge = run(provider.charge_saved_payment_method(charge_params))
    assert charge.provider_payment_id == "mock_pay_mock-charge-1"
    assert charge.status == "succeeded"
    run(
        provider.change_plan(
            ChangePlanParams(
                provider_subscription_id="mock_subscription",
                product_id="mock_next",
                proration_billing_mode="prorated_immediately",
                idempotency_key="mock-plan-1",
            )
        )
    )
    preview = run(
        provider.preview_change_plan(
            PreviewChangePlanParams(
                provider_subscription_id="mock_subscription",
                product_id="mock_next",
                proration_billing_mode="prorated_immediately",
            )
        )
    )
    assert preview.total_amount == 0
    run(provider.cancel_subscription("mock_subscription", "mock-cancel-1"))
    run(provider.reactivate_subscription("mock_subscription", "mock-reactivate-1"))
    run(
        provider.cancel_scheduled_plan_change(
            "mock_subscription",
            "mock-operation",
            idempotency_key="mock-release-1",
        )
    )
    invoice = run(provider.get_invoice_url("pay"))
    assert invoice is not None
    assert invoice.url == "https://example.com/invoice"
