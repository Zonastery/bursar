from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
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
    ProviderResolveUserFn,
    ProviderUrlResult,
    ResolveIdentityInput,
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
    "checkout.expired": "checkout.expired",
    "subscription.active": "subscription.created",
    "subscription.renewed": "subscription.renewed",
    "subscription.cancelled": "subscription.canceled",
    "subscription.expired": "subscription.expired",
    "subscription.failed": "subscription.updated",
    "subscription.on_hold": "subscription.updated",
    "subscription.updated": "subscription.updated",
    "subscription.cancellation_scheduled": "subscription.cancellation_scheduled",
    "subscription.cancellation_unscheduled": "subscription.cancellation_unscheduled",
    "subscription.plan_changed": "subscription.plan_changed",
    "payment.succeeded": "payment.succeeded",
    "payment.failed": "payment.failed",
    "refund.succeeded": "refund.created",
}


def _dodo_val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class DodoProvider(PaymentProvider):
    provider = "dodo"

    def __init__(
        self,
        get_client: Callable[[], Any],
        config: dict[str, str],
        sink: BillingEventSink,
        resolve_user: ProviderResolveUserFn | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        self._get_client = get_client
        self._config = config
        self._sink = sink
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
            session_kwargs["idempotency_key"] = params.idempotency_key
        session = await client.checkout_sessions.create(**session_kwargs)
        url = getattr(session, "checkout_url", None) or session.get("checkout_url")
        if not url:
            raise ValueError("Checkout session returned no URL")
        session_id = getattr(session, "session_id", None) or session.get("session_id")
        return CheckoutSessionResult(
            url=str(url),
            provider_session_id=str(session_id) if session_id else None,
        )

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        client = self._get_client()
        session = await client.customers.customer_portal.create(
            params.customer_id,
            return_url=params.return_url,
        )
        link = getattr(session, "link", None) or session.get("link")
        return ProviderUrlResult(url=str(link))

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        client = self._get_client()
        webhook_key = self._config.get("webhook_key")
        try:
            event = await client.webhooks.unwrap(
                req.raw_body,
                headers=req.headers,
                key=webhook_key,
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

        event_type = getattr(event, "type", None) or event.get("type", "")
        event_data = getattr(event, "data", None) or event.get("data", {})

        data_dict: dict[str, Any] = {}
        if hasattr(event_data, "model_dump"):
            data_dict = event_data.model_dump()
        elif isinstance(event_data, dict):
            data_dict = event_data
        elif hasattr(event_data, "__dict__"):
            data_dict = vars(event_data)
        else:
            with suppress(TypeError, ValueError):
                data_dict = dict(event_data)

        raw_metadata = data_dict.get("metadata", {})
        if hasattr(raw_metadata, "model_dump"):
            raw_metadata = raw_metadata.model_dump()
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}

        metadata = {str(k): str(v) for k, v in raw_metadata.items()}

        user_id: str | None = metadata.get("userId")
        if not user_id and self._resolve_user is not None and event_type not in ("payment.failed", "checkout.expired"):
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
            event_type,
            data_dict,
            user_id,
            metadata,
            self._sink,
            self._logger,
        )
        raw_event_id = next(
            (
                data_dict.get(key)
                for key in (
                    "id",
                    "payment_id",
                    "subscription_id",
                    "refund_id",
                    "dispute_id",
                )
                if data_dict.get(key) is not None
            ),
            None,
        )
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=str(raw_event_id) if raw_event_id is not None else None,
            event_type=str(event_type) or None,
        )

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {"cancel_at_next_billing_date": True}
        if idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
        await client.subscriptions.update(
            subscription_id,
            **kwargs,
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {"cancel_at_next_billing_date": False}
        if idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
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
            kwargs["idempotency_key"] = idempotency_key
        await client.subscriptions.cancel_change_plan(subscription_id, **kwargs)

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        client = self._get_client()
        try:
            session = await client.checkout_sessions.retrieve(provider_session_id)
        except Exception:
            return None
        status = getattr(session, "payment_status", None) or session.get("payment_status")
        return CheckoutSessionStatus.model_validate({"payment_status": status})

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        product_id = params.product_id or self._config.get("setup_product_id")
        if not product_id:
            raise ValueError("productId is required for payment method update")
        client = self._get_client()
        response = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"customer_id": params.customer_id},
            return_url=params.return_url,
            metadata={"purpose": "update_payment_method", "subscription_id": params.subscription_id},
        )
        url = getattr(response, "checkout_url", None) or response.get("checkout_url")
        if not url:
            raise ValueError("Failed to create payment method update session")
        return ProviderUrlResult(url=str(url))

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        product_id = params.product_id or self._config.get("setup_product_id")
        if not product_id:
            raise ValueError("setupProductId is required for payment method setup")
        client = self._get_client()
        session = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"customer_id": params.customer_id},
            return_url=params.return_url,
            metadata={"purpose": "setup_payment_method"},
        )
        url = getattr(session, "checkout_url", None) or session.get("checkout_url")
        if not url:
            raise ValueError("Checkout session returned no URL")
        return ProviderUrlResult(url=str(url))

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        client = self._get_client()
        response = await client.customers.retrieve_payment_methods(customer_id)
        items = response.get("items", []) if isinstance(response, dict) else getattr(response, "items", None) or []
        result: list[PaymentMethodInfo] = []
        for pm in items:
            pm_dict = pm.model_dump() if hasattr(pm, "model_dump") else pm
            if pm_dict.get("payment_method") != "card":
                continue
            card = pm_dict.get("card") or {}
            if not card.get("recurring_enabled", False):
                continue
            result.append(
                PaymentMethodInfo(
                    id=str(pm_dict.get("payment_method_id", "")),
                    last4=str(card.get("last4_digits", "")),
                    brand=str(card.get("card_network", "unknown")),
                    expiry_month=int(card.get("expiry_month", 0)),
                    expiry_year=int(card.get("expiry_year", 0)),
                )
            )
        return deduplicate_payment_methods(result)

    async def get_default_payment_method(self, customer_id: str) -> PaymentMethodInfo | None:
        methods = await self.list_payment_methods(customer_id)
        return methods[0] if len(methods) == 1 else None

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        preview = await self._get_client().checkout_sessions.preview(
            product_cart=[{"product_id": params.product_id, "quantity": params.quantity}],
            customer={"customer_id": params.customer_id},
        )
        breakup = _dodo_val(preview, "current_breakup", {}) or {}
        return SavedPaymentChargeQuote(
            amount_minor=int(_dodo_val(breakup, "total_amount", 0)),
            tax_minor=_dodo_val(breakup, "tax"),
            currency=str(_dodo_val(preview, "currency", "USD")),
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
            idempotency_key=params.idempotency_key,
        )
        payment_id = _dodo_val(session, "payment_id")
        if not payment_id:
            return SavedPaymentChargeResult(status="failed")
        payment = await client.payments.retrieve(payment_id)
        raw_status = _dodo_val(payment, "status", "processing")
        status = {
            "succeeded": "succeeded",
            "processing": "processing",
            "requires_customer_action": "requires_customer_action",
            "requires_payment_method": "requires_payment_method",
        }.get(raw_status, "failed")
        return SavedPaymentChargeResult.model_validate(
            {
                "provider_payment_id": str(payment_id),
                "status": status,
                "amount_minor": _dodo_val(payment, "total_amount"),
                "currency": _dodo_val(payment, "currency"),
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
        cid = getattr(customer, "customer_id", None) or customer.get("customer_id")
        return CreateCustomerResult(customer_id=str(cid))

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        client = self._get_client()
        payment = await client.payments.retrieve(provider_payment_id)
        link = getattr(payment, "payment_link", None) or payment.get("payment_link")
        if link:
            return ProviderUrlResult(url=str(link))
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
            kwargs["idempotency_key"] = params.idempotency_key
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
        immediate_charge = _dodo_val(response, "immediate_charge", {}) or {}
        line_items_raw = _dodo_val(immediate_charge, "line_items", []) or []
        summary = _dodo_val(immediate_charge, "summary", {}) or {}
        line_items: list[ChangePlanLineItem] = []
        for item in line_items_raw:
            if _dodo_val(item, "type") == "subscription":
                product_id = _dodo_val(item, "product_id")
                unit_price = _dodo_val(item, "unit_price")
                quantity = _dodo_val(item, "quantity")
                proration_factor = _dodo_val(item, "proration_factor")
                currency = _dodo_val(item, "currency")
                if not product_id or unit_price is None or quantity is None or proration_factor is None or not currency:
                    raise ValueError("Dodo plan-change preview returned an incomplete subscription item")
                line_items.append(
                    ChangePlanLineItem(
                        product_id=str(product_id),
                        name=str(_dodo_val(item, "name") or _dodo_val(item, "description", "")),
                        unit_price=int(unit_price),
                        quantity=int(quantity),
                        proration_factor=float(proration_factor),
                        currency=str(currency),
                        tax=int(_dodo_val(item, "tax", 0) or 0),
                        subtotal=0,
                    )
                )
        total = int(_dodo_val(summary, "total_amount", 0) or 0)
        settlement = int(_dodo_val(summary, "settlement_amount", 0) or 0)
        currency = str(_dodo_val(summary, "settlement_currency", "USD"))
        effective = str(_dodo_val(immediate_charge, "effective_at", ""))
        new_plan = _dodo_val(response, "new_plan")
        return ChangePlanPreview(
            total_amount=total,
            settlement_amount=settlement,
            currency=currency,
            line_items=line_items,
            effective_at=effective,
            recurring_amount=(
                int(_dodo_val(new_plan, "recurring_pre_tax_amount"))
                if new_plan is not None and _dodo_val(new_plan, "recurring_pre_tax_amount") is not None
                else None
            ),
            recurring_currency=(
                str(_dodo_val(new_plan, "currency"))
                if new_plan is not None and _dodo_val(new_plan, "currency") is not None
                else None
            ),
            next_billing_date=(
                str(_dodo_val(new_plan, "next_billing_date"))
                if new_plan is not None and _dodo_val(new_plan, "next_billing_date") is not None
                else None
            ),
            tax_amount=(
                int(_dodo_val(summary, "settlement_tax"))
                if _dodo_val(summary, "settlement_tax") is not None
                else int(_dodo_val(summary, "tax"))
                if _dodo_val(summary, "tax") is not None
                else None
            ),
            customer_credits=(
                int(_dodo_val(summary, "customer_credits"))
                if _dodo_val(summary, "customer_credits") is not None
                else None
            ),
        )
