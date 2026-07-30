from __future__ import annotations

import json
from datetime import UTC, datetime

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
from bursar.providers.types import (
    ChangePlanParams,
    ChangePlanPreview,
    CheckoutParams,
    CheckoutSessionResult,
    CreateCustomerParams,
    CreateCustomerResult,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderResolveUserFn,
    ProviderUrlResult,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    normalize_provider_logger,
)


class MockPaymentProvider(PaymentProvider):
    provider = "mock"

    def __init__(
        self,
        sink: BillingEventSink,
        resolve_user: ProviderResolveUserFn | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        self._sink = sink
        self._resolve_user = resolve_user
        self._logger = normalize_provider_logger(logger)

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        return CheckoutSessionResult(url=params.return_url)

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

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

    async def get_default_payment_method(
        self,
        customer_id: str,
    ) -> PaymentMethodInfo | None:
        methods = await self.list_payment_methods(customer_id)
        return methods[0] if methods else None

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        return SavedPaymentChargeResult(
            provider_payment_id=f"mock_pay_{params.idempotency_key}",
            status="succeeded",
            amount_minor=0,
            currency="USD",
        )

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        return SavedPaymentChargeQuote(amount_minor=0, currency="USD")

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        import time

        return CreateCustomerResult(customer_id=f"mock_cus_{int(time.time() * 1000)}")

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        return ProviderUrlResult(url="https://example.com/invoice")

    async def change_plan(self, params: ChangePlanParams) -> None:
        pass

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        return ChangePlanPreview(
            total_amount=0,
            settlement_amount=0,
            currency="USD",
            line_items=[],
            effective_at=datetime.now(UTC).isoformat(),
        )

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        try:
            payload = json.loads(req.raw_body)
        except (json.JSONDecodeError, ValueError):
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

        if not isinstance(payload, dict):
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

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
            self._logger,
        )

        event_type = str(payload.get("type", "")) or None
        raw_event_id = next(
            (
                data.get(key)
                for key in (
                    "id",
                    "payment_id",
                    "subscription_id",
                    "refund_id",
                    "dispute_id",
                )
                if data.get(key) is not None
            ),
            None,
        )
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=str(raw_event_id) if raw_event_id is not None else None,
            event_type=event_type,
        )
