from __future__ import annotations

import json

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
from bursar.providers.types import (
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    ProviderResolveUserFn,
    UpdatePaymentMethodParams,
    WebhookRequest,
)


class MockPaymentProvider(PaymentProvider):
    provider = "mock"

    def __init__(
        self,
        sink: BillingEventSink,
        resolve_user: ProviderResolveUserFn | None = None,
    ) -> None:
        self._sink = sink
        self._resolve_user = resolve_user

    async def create_checkout_session(self, params: CheckoutParams) -> dict:
        return {"url": params.return_url}

    async def create_customer_portal_session(self, params: PortalParams) -> dict:
        return {"url": params.return_url}

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> dict:
        return {"url": params.return_url}

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> dict:
        return {"url": params.return_url}

    async def cancel_subscription(self, subscription_id: str) -> None:
        pass

    async def reactivate_subscription(self, subscription_id: str) -> None:
        pass

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        return []

    async def create_customer(self, params: CreateCustomerParams) -> dict:
        import time

        return {"customerId": f"mock_cus_{int(time.time() * 1000)}"}

    async def get_invoice_url(self, provider_payment_id: str) -> dict | None:
        return {"url": "https://example.com/invoice"}

    async def handle_webhook(self, req: WebhookRequest) -> dict:
        try:
            payload = json.loads(req.raw_body)
        except (json.JSONDecodeError, ValueError):
            return {"received": False, "retryable": False}

        if not isinstance(payload, dict):
            return {"received": False, "retryable": False}

        data = payload.get("data", {}) or {}
        metadata = data.get("metadata", {}) or {}
        metadata = {str(k): str(v) for k, v in metadata.items()}
        user_id: str | None = metadata.get("userId")

        if not user_id and self._resolve_user:
            user_id = await self._resolve_user(data, metadata)

        await handle_dodo_billing_event(
            str(payload.get("type", "")),
            data,
            user_id,
            metadata,
            self._sink,
        )

        return {"received": True}
