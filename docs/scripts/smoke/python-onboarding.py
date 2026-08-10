"""Execute the Python billing-onboarding contract without network or database I/O."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import cast
from unittest.mock import MagicMock

import stripe
from bursar import (
    BillingEventType,
    BillingStore,
    Bursar,
    CommerceOptions,
    CreateCheckoutInput,
    CreditStore,
)
from bursar.providers import StripeProvider


async def main() -> None:
    webhook_secret = "whsec_docs_smoke"
    stripe_client = stripe.StripeClient("sk_test_docs_smoke")
    bursar = Bursar(
        credit_store=cast(CreditStore, object()),
        billing_store=cast(BillingStore, MagicMock(spec=BillingStore)),
        commerce_options=CommerceOptions(
            tenant_id="018f7f5f-7b4a-7000-8000-000000000001",
            provider_environment="test",
            default_provider="stripe",
            providers={
                "stripe": lambda context: StripeProvider(
                    get_client=lambda: stripe_client,
                    webhook_secret=webhook_secret,
                    event_sink=context.event_sink,
                ),
            },
        ),
    )

    checkout = CreateCheckoutInput(
        subject_id="actor_docs_smoke",
        account_id="account_docs_smoke",
        offer_key="pro_monthly",
        return_url="https://app.example.com/billing/success",
        cancel_url="https://app.example.com/billing",
        operation_key="checkout:docs-smoke",
    )
    assert checkout.account_id == "account_docs_smoke"

    raw_body = json.dumps(
        {
            "id": "evt_docs_smoke",
            "object": "event",
            "created": int(time.time()),
            "data": {"object": {"id": "pi_docs_smoke", "metadata": {}}},
            "livemode": False,
            "pending_webhooks": 1,
            "request": None,
            "type": "payment_intent.succeeded",
        },
        separators=(",", ":"),
    )
    timestamp = int(time.time())
    signature = hmac.new(
        webhook_secret.encode(),
        f"{timestamp}.{raw_body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    result = await bursar.require_commerce().handle_webhook(
        provider="stripe",
        raw_body=raw_body,
        headers={"stripe-signature": f"t={timestamp},v1={signature}"},
    )
    assert result.provider == "stripe"
    assert result.received is True
    assert result.retryable is False
    assert result.event_id == "evt_docs_smoke"
    assert result.event_type == "payment_intent.succeeded"
    assert BillingEventType.subscription_created.value == "subscription.created"
    assert BillingEventType.subscription_renewed.value == "subscription.renewed"


if __name__ == "__main__":
    asyncio.run(main())
