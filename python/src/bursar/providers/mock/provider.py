from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count

from bursar.billing.types import BillingEvent
from bursar.bursar import BillingEventSink
from bursar.providers.types import (
    ChangePlanParams,
    ChangePlanPreview,
    CheckoutParams,
    CheckoutSessionResult,
    CreateCustomerParams,
    CreateCustomerResult,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PortalParams,
    PreviewChangePlanParams,
    ProviderUrlResult,
    ResolveIdentityInput,
    ResolveUserCallback,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
)


class MockPaymentProvider:
    provider = "mock"

    def __init__(
        self,
        *,
        event_sink: BillingEventSink,
        resolve_user: ResolveUserCallback | None = None,
    ) -> None:
        self._sink = event_sink
        self._resolve_user = resolve_user
        self._customer_ids = count(1)
        self._customers_by_key: dict[str, str] = {}

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        return CheckoutSessionResult(url=params.return_url)

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        return ProviderUrlResult(url=params.return_url)

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        pass

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        pass

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        *,
        idempotency_key: str,
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

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        customer_id = self._customers_by_key.get(params.idempotency_key)
        if customer_id is None:
            customer_id = f"mock_cus_{next(self._customer_ids)}"
            self._customers_by_key[params.idempotency_key] = customer_id
        return CreateCustomerResult(customer_id=customer_id)

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

        event = BillingEvent.model_validate({**payload, "provider": self.provider})
        user_id = event.user_id

        if not user_id and self._resolve_user:
            metadata: dict[str, str] = {}
            for key, value in (event.metadata or {}).items():
                if not isinstance(value, str):
                    raise ValueError(f"mock billing event metadata.{key} must be a string")
                metadata[key] = value
            user_id = await self._resolve_user(
                ResolveIdentityInput(
                    provider=self.provider,
                    provider_event_type=event.event_type.value,
                    normalized_event_type=event.event_type.value,
                    customer_id=(event.customer.provider_customer_id if event.customer else None),
                    email=(event.customer.email if event.customer else None),
                    metadata=metadata,
                    successful=event.event_type.value
                    in {"payment.succeeded", "subscription.created", "subscription.renewed"},
                    checkout_kind=(
                        "subscription"
                        if event.event_type.value.startswith("subscription.")
                        else "credit_topup"
                        if metadata.get("credits")
                        else None
                    ),
                )
            )

        if user_id is not None:
            event = event.model_copy(update={"user_id": user_id})
        self._sink.ingest_billing_event(event)
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
