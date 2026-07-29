from __future__ import annotations

import inspect
import json
from dataclasses import fields
from pathlib import Path
from typing import Any, Literal

import pytest

from bursar.billing.types import CheckoutIntent, CheckoutIntentStatus
from bursar.commerce import (
    CommerceOptions,
    CommerceService,
    CreateCheckoutInput,
    UnknownOfferError,
)
from bursar.providers.types import (
    CheckoutParams,
    PaymentProvider,
    WebhookRequest,
    WebhookResult,
)

CATALOG = json.loads((Path(__file__).parents[2] / "common" / "commerce-parity.json").read_text())["catalog"]


class RecordingProvider(PaymentProvider):
    provider = "alpha"

    def __init__(self) -> None:
        self.checkout_params: list[CheckoutParams] = []

    async def create_checkout_session(self, params: CheckoutParams) -> dict:
        self.checkout_params.append(params)
        return {
            "url": f"https://checkout.example/{params.product_id}",
            "providerSessionId": "session-1",
        }

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        return WebhookResult(True, False, self.provider, "event-1", "payment.succeeded")


class FakeBilling:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get_active_bursar_config(self) -> dict[str, Any]:
        return CATALOG

    def get_blocking_subscription(self, account_id: str | None) -> None:
        return None

    def get_customer_by_user_id(
        self,
        account_id: str | None,
        provider: str | None = None,
    ) -> None:
        return None

    def create_or_get_checkout_intent(
        self,
        subject_id: str,
        provider: str,
        checkout_kind: Literal["subscription", "credit_topup"],
        product_key: str,
        request_digest: str,
        expires_at: str,
    ) -> CheckoutIntent:
        return CheckoutIntent(
            id="intent-1",
            subject_id=subject_id,
            provider=provider,
            checkout_kind=checkout_kind,
            product_key=product_key,
            request_digest=request_digest,
            status=CheckoutIntentStatus.open,
            expires_at=expires_at,
        )

    def update_checkout_intent(self, intent_id: str, **updates: Any) -> None:
        self.updates.append((intent_id, updates))


def service(provider: RecordingProvider) -> CommerceService:
    return CommerceService(
        FakeBilling(),
        object(),
        object(),
        CommerceOptions(providers={"alpha": lambda _context: provider}),
    )


@pytest.mark.asyncio
async def test_checkout_resolves_offer_key_before_calling_provider() -> None:
    provider = RecordingProvider()

    result = await service(provider).create_checkout(
        CreateCheckoutInput(
            subject_id="subject-1",
            account_id="account-1",
            offer_key="starter_month",
            return_url="https://app.example/return?intent={intentId}",
            cancel_url="https://app.example/cancel?intent={intentId}",
            operation_key="operation-1",
        )
    )

    assert result.offer_key == "starter_month"
    assert result.provider == "alpha"
    assert result.url == "https://checkout.example/alpha-starter-month"
    assert len(provider.checkout_params) == 1
    assert provider.checkout_params[0].product_id == "alpha-starter-month"
    assert provider.checkout_params[0].return_url.endswith("intent=intent-1")


@pytest.mark.asyncio
async def test_checkout_rejects_unknown_catalog_offer() -> None:
    with pytest.raises(UnknownOfferError):
        await service(RecordingProvider()).create_checkout(
            CreateCheckoutInput(
                subject_id="subject-1",
                offer_key="alpha-starter-month",
                return_url="https://app.example/return",
                cancel_url="https://app.example/cancel",
                operation_key="operation-1",
            )
        )


def test_public_commerce_inputs_do_not_expose_provider_product_ids() -> None:
    checkout_fields = {field.name for field in fields(CreateCheckoutInput)}
    assert "offer_key" in checkout_fields
    assert "product_id" not in checkout_fields
    assert "product_id" not in inspect.signature(CommerceService.preview_plan_change).parameters
    assert "product_id" not in inspect.signature(CommerceService.confirm_plan_change).parameters
