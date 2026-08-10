"""Coverage for current Stripe webhook object shapes and lifecycle events."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from stripe import StripeClient

from bursar.billing.types import BillingEventResult
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event

STRIPE_EVENT_CREATED = 1_767_225_600


def subscription_fixture() -> dict:
    return {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "cancel_at_period_end": False,
        "metadata": {"bursar_account_id": "u1"},
        "trial_end": None,
        "cancel_at": None,
        "ended_at": None,
        "items": {
            "data": [
                {
                    "id": "si_1",
                    "current_period_start": 1_764_547_200,
                    "current_period_end": 1_767_225_600,
                    "price": {"id": "price_pro", "product": "prod_pro"},
                }
            ]
        },
    }


def stripe_client(
    *,
    checkout_retrieve: AsyncMock | None = None,
    subscription_retrieve: AsyncMock | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        v1=SimpleNamespace(
            checkout=SimpleNamespace(
                sessions=SimpleNamespace(
                    retrieve_async=checkout_retrieve
                    or AsyncMock(
                        return_value={
                            "line_items": {
                                "data": [
                                    {
                                        "price": {
                                            "id": "price_topup",
                                            "product": "prod_topup",
                                        }
                                    }
                                ]
                            }
                        }
                    )
                )
            ),
            subscriptions=SimpleNamespace(
                retrieve_async=subscription_retrieve or AsyncMock(return_value=subscription_fixture())
            ),
        )
    )


@pytest.fixture
def sink() -> MagicMock:
    value = MagicMock()
    value.ingest_billing_event.return_value = BillingEventResult(handled=True)
    return value


async def emit(
    event_type: str,
    data: dict,
    sink: MagicMock,
    stripe: SimpleNamespace,
    *,
    event_id: str | None = None,
) -> None:
    await handle_stripe_billing_event(
        event_type,
        event_id or f"evt_{event_type}",
        data,
        None,
        {},
        sink,
        cast(StripeClient, stripe),
        event_created=STRIPE_EVENT_CREATED,
    )


@pytest.mark.asyncio
async def test_current_subscription_periods_and_invoice_parent_references(sink: MagicMock) -> None:
    subscription = subscription_fixture()
    stripe = stripe_client()

    await emit("customer.subscription.updated", subscription, sink, stripe)
    await emit(
        "customer.subscription.deleted",
        {**subscription, "status": "canceled", "ended_at": STRIPE_EVENT_CREATED},
        sink,
        stripe,
    )
    await emit(
        "invoice.paid",
        {
            "id": "in_1",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {
                    "subscription": "sub_1",
                    "metadata": {"bursar_account_id": "u1", "source": "subscription"},
                },
            },
            "customer": "cus_1",
            "metadata": {"invoice_key": "invoice_value"},
            "amount_paid": 1000,
            "amount_due": 1000,
            "currency": "usd",
            "period_start": 1_764_547_200,
            "period_end": 1_767_225_600,
        },
        sink,
        stripe,
    )

    events = [call.args[0] for call in sink.ingest_billing_event.call_args_list]
    assert [event.event_type for event in events] == [
        "subscription.updated",
        "subscription.canceled",
        "invoice.paid",
    ]
    assert events[0].subscription.period_start == "2025-12-01T00:00:00+00:00"
    assert events[0].subscription.period_end == "2026-01-01T00:00:00+00:00"
    assert events[0].subscription.refs.price_id == "price_pro"
    assert events[0].subscription.refs.product_id == "prod_pro"
    assert events[2].account_id == "u1"
    assert events[2].metadata == {
        "bursar_account_id": "u1",
        "source": "subscription",
        "invoice_key": "invoice_value",
    }
    assert events[2].invoice.provider_invoice_id == "in_1"
    assert events[2].invoice.currency == "USD"


@pytest.mark.asyncio
async def test_delayed_checkout_events_wait_and_separate_tax(sink: MagicMock) -> None:
    checkout_retrieve = AsyncMock(
        return_value={"line_items": {"data": [{"price": {"id": "price_topup", "product": "prod_topup"}}]}}
    )
    stripe = stripe_client(checkout_retrieve=checkout_retrieve)
    session = {
        "id": "cs_1",
        "client_reference_id": "u1",
        "mode": "payment",
        "customer": "cus_1",
        "customer_details": {"email": "u1@example.com"},
        "payment_intent": "pi_1",
        "amount_subtotal": 1000,
        "amount_total": 1180,
        "total_details": {"amount_tax": 180},
        "currency": "usd",
        "metadata": {"checkout_intent_id": "intent_1"},
    }

    await emit(
        "checkout.session.completed",
        {**session, "payment_status": "unpaid"},
        sink,
        stripe,
    )
    sink.ingest_billing_event.assert_not_called()
    checkout_retrieve.assert_not_awaited()

    await emit(
        "checkout.session.async_payment_succeeded",
        {**session, "payment_status": "paid"},
        sink,
        stripe,
    )
    succeeded = sink.ingest_billing_event.call_args_list[0].args[0]
    assert succeeded.event_type == "payment.succeeded"
    assert succeeded.account_id == "u1"
    assert succeeded.customer.provider_customer_id == "cus_1"
    assert succeeded.customer.email == "u1@example.com"
    assert succeeded.metadata == {"checkout_intent_id": "intent_1"}
    assert succeeded.payment.provider_payment_id == "pi_1"
    assert succeeded.payment.amount_minor == 1000
    assert succeeded.payment.tax_minor == 180
    assert succeeded.payment.currency == "USD"
    assert succeeded.payment.refs.price_id == "price_topup"
    assert succeeded.payment.refs.product_id == "prod_topup"

    await emit(
        "checkout.session.async_payment_failed",
        {
            **session,
            "id": "cs_2",
            "payment_intent": "pi_2",
            "payment_status": "unpaid",
        },
        sink,
        stripe,
    )
    failed = sink.ingest_billing_event.call_args_list[1].args[0]
    assert failed.event_type == "payment.failed"
    assert failed.payment.provider_payment_id == "pi_2"
    assert failed.payment.status == "failed"


@pytest.mark.asyncio
async def test_subscription_checkout_and_expiration_use_current_shapes(sink: MagicMock) -> None:
    stripe = stripe_client()
    await emit(
        "checkout.session.completed",
        {
            "id": "cs_sub",
            "client_reference_id": "u1",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_1",
            "customer": "cus_1",
            "metadata": {"plan_slug": "pro", "checkout_intent_id": "intent_sub"},
        },
        sink,
        stripe,
    )
    await emit(
        "checkout.session.expired",
        {
            "id": "cs_expired",
            "client_reference_id": "u1",
            "customer": "cus_1",
            "metadata": {"checkout_intent_id": "intent_expired"},
        },
        sink,
        stripe,
    )

    completed, expired = [call.args[0] for call in sink.ingest_billing_event.call_args_list]
    assert completed.event_type == "checkout.completed"
    assert completed.subscription.provider_subscription_id == "sub_1"
    assert completed.subscription.period_start == "2025-12-01T00:00:00+00:00"
    assert completed.subscription.period_end == "2026-01-01T00:00:00+00:00"
    assert completed.subscription.refs.lookup_key == "pro"
    assert completed.metadata["checkout_intent_id"] == "intent_sub"
    assert expired.event_type == "checkout.expired"
    assert expired.metadata == {"checkout_intent_id": "intent_expired"}


@pytest.mark.asyncio
async def test_failed_invoice_maps_to_canonical_failed_payment(sink: MagicMock) -> None:
    await emit(
        "invoice.payment_failed",
        {
            "id": "in_failed",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {
                    "subscription": "sub_1",
                    "metadata": {"bursar_account_id": "u1"},
                },
            },
            "customer": "cus_1",
            "subtotal": 1000,
            "total_taxes": [{"amount": 180}],
            "currency": "usd",
            "payments": {
                "data": [
                    {
                        "payment": {
                            "type": "payment_intent",
                            "payment_intent": "pi_failed",
                        }
                    }
                ]
            },
        },
        sink,
        stripe_client(),
    )
    event = sink.ingest_billing_event.call_args.args[0]
    assert event.event_type == "payment.failed"
    assert event.payment.provider_payment_id == "pi_failed"
    assert event.payment.amount_minor == 1000
    assert event.payment.tax_minor == 180
    assert event.payment.currency == "USD"
    assert event.payment.refs.price_id == "price_pro"


@pytest.mark.asyncio
async def test_stripe_retrieval_failure_propagates_for_webhook_retry(sink: MagicMock) -> None:
    error = RuntimeError("Stripe temporarily unavailable")
    stripe = stripe_client(checkout_retrieve=AsyncMock(side_effect=error))

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await emit(
            "checkout.session.completed",
            {
                "id": "cs_retry",
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": "pi_retry",
                "amount_subtotal": 1000,
                "amount_total": 1000,
                "currency": "usd",
            },
            sink,
            stripe,
        )
    sink.ingest_billing_event.assert_not_called()


@pytest.mark.parametrize(
    ("provider_event", "provider_status", "expected_event", "expected_status"),
    [
        ("payment_intent.succeeded", "succeeded", "payment.succeeded", "succeeded"),
        (
            "payment_intent.payment_failed",
            "requires_payment_method",
            "payment.failed",
            "failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_payment_intent_event_uses_webhook_type_for_outcome(
    provider_event: str,
    provider_status: str,
    expected_event: str,
    expected_status: str,
    sink: MagicMock,
) -> None:
    await emit(
        provider_event,
        {
            "id": f"pi_{expected_status}",
            "amount": 500,
            "currency": "usd",
            "status": provider_status,
            "metadata": {
                "auto_recharge_attempt_id": "attempt_1",
                "bursar_account_id": "u1",
                "price_id": "price_topup",
            },
        },
        sink,
        SimpleNamespace(),
    )
    event = sink.ingest_billing_event.call_args.args[0]
    assert event.event_type == expected_event
    assert event.payment.status == expected_status
    assert event.payment.refs.price_id == "price_topup"


@pytest.mark.parametrize(
    ("provider_event", "provider_status", "expected_event", "expected_status"),
    [
        ("refund.created", "pending", "refund.created", "pending"),
        ("refund.updated", "succeeded", "refund.updated", "succeeded"),
        ("refund.failed", "pending", "refund.failed", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_refund_event_preserves_lifecycle_state(
    provider_event: str,
    provider_status: str,
    expected_event: str,
    expected_status: str,
    sink: MagicMock,
) -> None:
    await emit(
        provider_event,
        {
            "id": f"re_{expected_status}",
            "payment_intent": "pi_1",
            "amount": 500,
            "currency": "usd",
            "status": provider_status,
            "metadata": {"bursar_account_id": "u1"},
        },
        sink,
        SimpleNamespace(),
    )
    event = sink.ingest_billing_event.call_args.args[0]
    assert event.event_type == expected_event
    assert event.refund.status == expected_status


@pytest.mark.asyncio
async def test_unknown_stripe_event_is_ignored(sink: MagicMock) -> None:
    await emit("charge.succeeded", {}, sink, SimpleNamespace())
    sink.ingest_billing_event.assert_not_called()
