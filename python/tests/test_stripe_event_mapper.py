"""Table-driven coverage for every supported Stripe webhook route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bursar.billing.models import BillingEventResult
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event


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
            retrieve_async=AsyncMock(return_value={"metadata": {"userId": "u1"}, "status": "active"})
        ),
    )
    await handle_stripe_billing_event(event_type, f"evt_{event_type}", data, None, {}, sink, stripe)
    assert sink.ingest_billing_event.call_args is not None
    assert sink.ingest_billing_event.call_args.args[0].event_type == expected


@pytest.mark.asyncio
async def test_checkout_subscription_emits_created_and_activated(sink: MagicMock) -> None:
    stripe = SimpleNamespace(
        checkout=SimpleNamespace(
            Session=SimpleNamespace(retrieve_async=AsyncMock(return_value={"line_items": {"data": []}}))
        ),
        Subscription=SimpleNamespace(
            retrieve_async=AsyncMock(
                return_value={"status": "active", "current_period_start": 1764547200, "current_period_end": 1767225600}
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
    await handle_stripe_billing_event("checkout.session.completed", "evt_checkout", data, "u1", {}, sink, stripe)
    assert [call.args[0].event_type for call in sink.ingest_billing_event.call_args_list] == [
        "subscription.created",
        "subscription.activated",
    ]


@pytest.mark.asyncio
async def test_unknown_stripe_event_is_ignored(sink: MagicMock) -> None:
    await handle_stripe_billing_event("charge.succeeded", "evt_unknown", {}, None, {}, sink, SimpleNamespace())
    sink.ingest_billing_event.assert_not_called()
