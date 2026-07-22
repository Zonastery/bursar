from __future__ import annotations

import json

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
from bursar.providers.types import (
    ChangePlanParams,
    ChangePlanPreview,
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    ProviderResolveUserFn,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
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

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        pass

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        pass

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        pass

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        return []

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        return SavedPaymentChargeResult(
            provider_payment_id=f"mock_pay_{params.idempotency_key}",
            status="succeeded",
            amount_minor=0,
            currency="USD",
        )

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        return SavedPaymentChargeQuote(amount_minor=0, currency="USD")

    async def create_customer(self, params: CreateCustomerParams) -> dict:
        import time

        return {"customerId": f"mock_cus_{int(time.time() * 1000)}"}

    async def get_invoice_url(self, provider_payment_id: str) -> dict | None:
        return {"url": "https://example.com/invoice"}

    async def change_plan(self, params: ChangePlanParams) -> None:
        pass

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        return ChangePlanPreview(
            total_amount=0,
            settlement_amount=0,
            currency="USD",
            line_items=None,
            effective_at="",
        )

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
