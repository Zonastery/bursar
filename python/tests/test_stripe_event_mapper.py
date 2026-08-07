"""Table-driven coverage for every supported Stripe webhook route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bursar.billing.types import BillingEventResult
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event

STRIPE_EVENT_CREATED = 1_767_225_600


@pytest.fixture
def sink() -> MagicMock:
    value = MagicMock()
    value.ingest_billing_event.return_value = BillingEventResult(handled=True)
    return value


@pytest.mark.parametrize(
    ("event_type", "data", "expected"),
    [
        ("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}, "subscription.canceled"),
        (
            "customer.subscription.updated",
            {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "metadata": {"userId": "u1"},
                "current_period_end": 1767225600,
            },
            "subscription.updated",
        ),
        (
            "invoice.paid",
            {
                "id": "in_1",
                "subscription": "sub_1",
                "customer": "cus_1",
                "metadata": {"userId": "u1"},
                "amount_paid": 1000,
                "amount_due": 1000,
                "currency": "usd",
            },
            "invoice.paid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_supported_stripe_routes_emit_canonical_events(
    event_type: str, data: dict, expected: str, sink: MagicMock
) -> None:
    stripe = SimpleNamespace(
        subscriptions=SimpleNamespace(retrieve_async=AsyncMock()),
        Subscription=SimpleNamespace(
            retrieve_async=AsyncMock(return_value={"id": "sub_1", "metadata": {"userId": "u1"}, "status": "active"})
        ),
    )
    await handle_stripe_billing_event(
        event_type,
        f"evt_{event_type}",
        data,
        None,
        {},
        sink,
        stripe,
        event_created=STRIPE_EVENT_CREATED,
    )
    assert sink.ingest_billing_event.call_args is not None
    assert sink.ingest_billing_event.call_args.args[0].event_type == expected


@pytest.mark.asyncio
async def test_checkout_subscription_emits_checkout_completed(sink: MagicMock) -> None:
    stripe = SimpleNamespace(
        checkout=SimpleNamespace(
            Session=SimpleNamespace(retrieve_async=AsyncMock(return_value={"line_items": {"data": []}}))
        ),
        Subscription=SimpleNamespace(
            retrieve_async=AsyncMock(
                return_value={
                    "id": "sub_1",
                    "status": "active",
                    "current_period_start": 1764547200,
                    "current_period_end": 1767225600,
                }
            )
        ),
    )
    data = {
        "id": "cs_1",
        "mode": "subscription",
        "subscription": "sub_1",
        "customer": "cus_1",
        "metadata": {"plan_slug": "pro"},
    }
    await handle_stripe_billing_event(
        "checkout.session.completed",
        "evt_checkout",
        data,
        "u1",
        {},
        sink,
        stripe,
        event_created=STRIPE_EVENT_CREATED,
    )
    assert [call.args[0].event_type for call in sink.ingest_billing_event.call_args_list] == ["checkout.completed"]


@pytest.mark.parametrize(
    ("provider_event", "provider_status", "expected_event", "expected_status"),
    [
        ("payment_intent.succeeded", "succeeded", "payment.succeeded", "succeeded"),
        ("payment_intent.payment_failed", "requires_payment_method", "payment.failed", "failed"),
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
    await handle_stripe_billing_event(
        provider_event,
        f"evt_{expected_status}",
        {
            "id": f"pi_{expected_status}",
            "amount": 500,
            "currency": "usd",
            "status": provider_status,
            "metadata": {
                "auto_recharge_attempt_id": "attempt_1",
                "userId": "u1",
                "price_id": "price_topup",
            },
        },
        None,
        {},
        sink,
        SimpleNamespace(),
        event_created=STRIPE_EVENT_CREATED,
    )
    event = sink.ingest_billing_event.call_args.args[0]
    assert event.event_type == expected_event
    assert event.payment is not None
    assert event.payment.status == expected_status
    assert event.payment.refs is not None
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
    await handle_stripe_billing_event(
        provider_event,
        f"evt_{expected_status}",
        {
            "id": f"re_{expected_status}",
            "payment_intent": "pi_1",
            "amount": 500,
            "currency": "usd",
            "status": provider_status,
            "metadata": {"userId": "u1"},
        },
        None,
        {},
        sink,
        SimpleNamespace(),
        event_created=STRIPE_EVENT_CREATED,
    )
    event = sink.ingest_billing_event.call_args.args[0]
    assert event.event_type == expected_event
    assert event.refund is not None
    assert event.refund.status == expected_status


@pytest.mark.asyncio
async def test_unknown_stripe_event_is_ignored(sink: MagicMock) -> None:
    await handle_stripe_billing_event(
        "charge.succeeded",
        "evt_unknown",
        {},
        None,
        {},
        sink,
        SimpleNamespace(),
        event_created=STRIPE_EVENT_CREATED,
    )
    sink.ingest_billing_event.assert_not_called()
