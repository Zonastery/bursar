"""Hermetic provider-adapter contract tests (no vendor network calls)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from dodopayments import AsyncDodoPayments
from stripe import StripeClient

from bursar.billing.types import BillingEvent, BillingEventResult, BillingEventType, BillingInvoiceInfo
from bursar.errors import StoreUnavailableError
from bursar.providers._shared import call_billing_event_sink
from bursar.providers.dodo.provider import DodoProvider
from bursar.providers.mock.provider import MockPaymentProvider
from bursar.providers.stripe.provider import StripeProvider, _scoped_idempotency_key
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
    invoice = run(provider.get_invoice_url("pay"))
    assert invoice is not None
    assert invoice.url == "https://example.com/invoice"
