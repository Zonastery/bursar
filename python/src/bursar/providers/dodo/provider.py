from __future__ import annotations

import logging
import math
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import dodo_billing_event_id, handle_dodo_billing_event
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutSessionResult,
    CheckoutSessionStatus,
    CreateCustomerParams,
    CreateCustomerResult,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderUrlResult,
    ResolveIdentityInput,
    ResolveUserCallback,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    deduplicate_payment_methods,
    normalize_provider_logger,
)

logger = logging.getLogger(__name__)

_NORMALIZED_EVENT_TYPES = {
    "subscription.active": "subscription.created",
    "subscription.renewed": "subscription.renewed",
    "subscription.cancelled": "subscription.canceled",
    "subscription.expired": "subscription.expired",
    "subscription.failed": "subscription.updated",
    "subscription.on_hold": "subscription.updated",
    "subscription.updated": "subscription.updated",
    "subscription.plan_changed": "subscription.plan_changed",
    "payment.succeeded": "payment.succeeded",
    "payment.failed": "payment.failed",
    "refund.succeeded": "refund.created",
    "refund.failed": "refund.failed",
    "dispute.opened": "dispute.created",
    "dispute.challenged": "dispute.created",
    "dispute.won": "dispute.closed",
    "dispute.lost": "dispute.closed",
    "dispute.accepted": "dispute.closed",
    "dispute.cancelled": "dispute.closed",
    "dispute.expired": "dispute.closed",
}

_BURSAR_METADATA_KEYS = {
    "userId",
    "plan_slug",
    "billing_interval",
    "credits",
    "checkout_intent_id",
}


def _normalize_metadata(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Dodo webhook metadata must be an object")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("Dodo webhook metadata keys must be strings")
        if not isinstance(item, str | int | float | bool) or (isinstance(item, float) and not math.isfinite(item)):
            raise ValueError(f"Dodo webhook metadata.{key} must be a scalar value")
        if key in _BURSAR_METADATA_KEYS and not isinstance(item, str):
            raise ValueError(f"Dodo webhook metadata.{key} must be a string")
        normalized[key] = str(item)
    return normalized


def _idempotency_headers(key: str | None) -> dict[str, str] | None:
    return {"Idempotency-Key": key} if key else None


class DodoProvider(PaymentProvider):
    provider = "dodo"

    def __init__(
        self,
        *,
        get_client: Callable[[], Any],
        webhook_key: str,
        event_sink: BillingEventSink,
        setup_product_id: str | None = None,
        resolve_user: ResolveUserCallback | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        if not webhook_key.strip():
            raise ValueError("webhook_key must not be empty")
        self._get_client = get_client
        self._webhook_key = webhook_key
        self._setup_product_id = setup_product_id
        self._sink = event_sink
        self._resolve_user = resolve_user
        self._logger = normalize_provider_logger(logger)

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        client = self._get_client()
        quantity = params.quantity if params.quantity is not None else 1
        session_kwargs: dict[str, Any] = {
            "product_cart": [{"product_id": params.product_id, "quantity": quantity}],
            "return_url": params.return_url,
        }
        if params.cancel_url:
            session_kwargs["cancel_url"] = params.cancel_url
        if params.metadata:
            session_kwargs["metadata"] = params.metadata
        if params.customer_id:
            session_kwargs["customer"] = {"customer_id": params.customer_id}
        elif params.email:
            session_kwargs["customer"] = {"email": params.email}

        if params.idempotency_key:
            session_kwargs["extra_headers"] = _idempotency_headers(params.idempotency_key)
        session = await client.checkout_sessions.create(**session_kwargs)
        url = session.checkout_url
        if not url:
            raise ValueError("Checkout session returned no URL")
        return CheckoutSessionResult(
            url=str(url),
            provider_session_id=str(session.session_id),
        )

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        client = self._get_client()
        session = await client.customers.customer_portal.create(
            params.customer_id,
            return_url=params.return_url,
        )
        return ProviderUrlResult(url=str(session.link))

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        client = self._get_client()
        try:
            event = client.webhooks.unwrap(
                req.raw_body,
                headers=req.headers,
                key=self._webhook_key,
            )
        except Exception as exc:
            self._logger.warning("Dodo webhook verification failed", {"error": str(exc)})
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

        event_type = event.type
        data_dict = event.data.model_dump(mode="json")

        metadata = _normalize_metadata(data_dict.get("metadata"))

        user_id: str | None = metadata.get("userId")
        if not user_id and self._resolve_user is not None and event_type != "payment.failed":
            customer = data_dict.get("customer")
            customer_dict = customer if isinstance(customer, dict) else {}
            customer_id = str(data_dict.get("customer_id") or customer_dict.get("customer_id") or "").strip() or None
            email_value = customer_dict.get("email")
            email = str(email_value).strip().lower() if email_value else None
            provider_event_type = str(event_type)
            user_id = await self._resolve_user(
                ResolveIdentityInput(
                    provider=self.provider,
                    provider_event_type=provider_event_type,
                    normalized_event_type=_NORMALIZED_EVENT_TYPES.get(provider_event_type),
                    customer_id=customer_id,
                    email=email,
                    metadata=metadata,
                    successful=provider_event_type
                    in {"payment.succeeded", "subscription.active", "subscription.renewed"},
                    checkout_kind=(
                        "subscription"
                        if provider_event_type.startswith("subscription.")
                        else "credit_topup"
                        if metadata.get("credits")
                        else None
                    ),
                )
            )

        await handle_dodo_billing_event(
            event_type=str(event_type),
            data=data_dict,
            event_timestamp=event.timestamp,
            user_id=user_id,
            metadata=metadata,
            sink=self._sink,
            logger=self._logger,
        )
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=dodo_billing_event_id(str(event_type), data_dict, event.timestamp),
            event_type=str(event_type) or None,
        )

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {"cancel_at_next_billing_date": True}
        if idempotency_key:
            kwargs["extra_headers"] = _idempotency_headers(idempotency_key)
        await client.subscriptions.update(
            subscription_id,
            **kwargs,
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {"cancel_at_next_billing_date": False}
        if idempotency_key:
            kwargs["extra_headers"] = _idempotency_headers(idempotency_key)
        await client.subscriptions.update(
            subscription_id,
            **kwargs,
        )

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {}
        if idempotency_key:
            kwargs["extra_headers"] = _idempotency_headers(idempotency_key)
        await client.subscriptions.cancel_change_plan(subscription_id, **kwargs)

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        from dodopayments import NotFoundError

        client = self._get_client()
        try:
            session = await client.checkout_sessions.retrieve(provider_session_id)
        except NotFoundError:
            return None
        return CheckoutSessionStatus(payment_status=session.payment_status)

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        product_id = params.product_id or self._setup_product_id
        if not product_id:
            raise ValueError("productId is required for payment method update")
        client = self._get_client()
        response = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"customer_id": params.customer_id},
            return_url=params.return_url,
            metadata={"purpose": "update_payment_method", "subscription_id": params.subscription_id},
        )
        url = response.checkout_url
        if not url:
            raise ValueError("Failed to create payment method update session")
        return ProviderUrlResult(url=str(url))

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        product_id = params.product_id or self._setup_product_id
        if not product_id:
            raise ValueError("setupProductId is required for payment method setup")
        client = self._get_client()
        session = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"customer_id": params.customer_id},
            return_url=params.return_url,
            metadata={"purpose": "setup_payment_method"},
        )
        url = session.checkout_url
        if not url:
            raise ValueError("Checkout session returned no URL")
        return ProviderUrlResult(url=str(url))

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        client = self._get_client()
        response = await client.customers.retrieve_payment_methods(customer_id)
        result: list[PaymentMethodInfo] = []
        for payment_method in response.items:
            if payment_method.payment_method != "card" or payment_method.card is None:
                continue
            if not payment_method.recurring_enabled:
                continue
            card = payment_method.card
            result.append(
                PaymentMethodInfo(
                    id=payment_method.payment_method_id,
                    last4=card.last4_digits or "",
                    brand=card.card_network or "unknown",
                    expiry_month=int(card.expiry_month or 0),
                    expiry_year=int(card.expiry_year or 0),
                )
            )
        methods = deduplicate_payment_methods(result)
        if len(methods) == 1:
            methods[0] = methods[0].model_copy(update={"is_default": True})
        return methods

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        preview = await self._get_client().checkout_sessions.preview(
            product_cart=[{"product_id": params.product_id, "quantity": params.quantity}],
            customer={"customer_id": params.customer_id},
        )
        return SavedPaymentChargeQuote(
            amount_minor=preview.current_breakup.total_amount,
            tax_minor=preview.current_breakup.tax,
            currency=str(preview.currency),
        )

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        client = self._get_client()
        session = await client.checkout_sessions.create(
            product_cart=[{"product_id": params.product_id, "quantity": params.quantity}],
            customer={"customer_id": params.customer_id},
            payment_method_id=params.payment_method_id,
            confirm=True,
            return_url=params.return_url,
            metadata=params.metadata or {},
            extra_headers=_idempotency_headers(params.idempotency_key),
        )
        payment_id = session.payment_id
        if not payment_id:
            return SavedPaymentChargeResult(status="failed")
        payment = await client.payments.retrieve(payment_id)
        raw_status = payment.status or "processing"
        status = {
            "succeeded": "succeeded",
            "processing": "processing",
            "requires_customer_action": "requires_customer_action",
            "requires_payment_method": "requires_payment_method",
        }.get(raw_status, "failed")
        return SavedPaymentChargeResult.model_validate(
            {
                "provider_payment_id": payment.payment_id,
                "status": status,
                "amount_minor": payment.total_amount,
                "currency": payment.currency,
            }
        )

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "email": params.email,
            "name": params.name,
        }
        if params.metadata:
            kwargs["metadata"] = params.metadata
        customer = await client.customers.create(**kwargs)
        return CreateCustomerResult(customer_id=customer.customer_id)

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        client = self._get_client()
        payment = await client.payments.retrieve(provider_payment_id)
        if payment.payment_link:
            return ProviderUrlResult(url=payment.payment_link)
        return None

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "product_id": params.product_id,
            "proration_billing_mode": params.proration_billing_mode,
            "quantity": params.quantity,
        }
        if params.effective_at:
            kwargs["effective_at"] = params.effective_at
        if params.on_payment_failure:
            kwargs["on_payment_failure"] = params.on_payment_failure
        if params.metadata:
            kwargs["metadata"] = params.metadata
        if params.idempotency_key:
            kwargs["extra_headers"] = _idempotency_headers(params.idempotency_key)
        await client.subscriptions.change_plan(params.provider_subscription_id, **kwargs)
        return ChangePlanResult(provider_operation_id=None)

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "product_id": params.product_id,
            "proration_billing_mode": params.proration_billing_mode,
            "quantity": params.quantity,
        }
        if params.effective_at:
            kwargs["effective_at"] = params.effective_at
        response = await client.subscriptions.preview_change_plan(params.provider_subscription_id, **kwargs)
        immediate_charge = response.immediate_charge
        summary = immediate_charge.summary
        line_items: list[ChangePlanLineItem] = []
        for item in immediate_charge.line_items:
            if item.type == "subscription":
                line_items.append(
                    ChangePlanLineItem(
                        product_id=item.product_id,
                        name=item.name or item.description or "",
                        unit_price=item.unit_price,
                        quantity=item.quantity,
                        proration_factor=item.proration_factor,
                        currency=str(item.currency),
                        tax=item.tax or 0,
                        subtotal=int(
                            (Decimal(item.unit_price * item.quantity) * Decimal(str(item.proration_factor))).quantize(
                                Decimal("1"), rounding=ROUND_HALF_UP
                            )
                        ),
                    )
                )
        new_plan = response.new_plan
        return ChangePlanPreview(
            total_amount=summary.total_amount,
            settlement_amount=summary.settlement_amount,
            currency=str(summary.settlement_currency),
            line_items=line_items,
            effective_at=immediate_charge.effective_at.isoformat(),
            recurring_amount=(
                new_plan.recurring_pre_tax_amount if new_plan.recurring_pre_tax_amount is not None else None
            ),
            recurring_currency=str(new_plan.currency),
            next_billing_date=new_plan.next_billing_date.isoformat(),
            tax_amount=(
                summary.settlement_tax
                if summary.settlement_tax is not None
                else summary.tax
                if summary.tax is not None
                else None
            ),
            customer_credits=summary.customer_credits,
        )
