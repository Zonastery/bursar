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
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderResolveUserFn,
    UpdatePaymentMethodParams,
    WebhookRequest,
)

logger = logging.getLogger(__name__)


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
        self._logger = logger

    async def create_checkout_session(self, params: CheckoutParams) -> dict:
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

        session = await client.checkout_sessions.create(**session_kwargs)
        url = getattr(session, "checkout_url", None) or session.get("checkout_url")
        if not url:
            raise ValueError("Checkout session returned no URL")
        session_id = getattr(session, "session_id", None) or session.get("session_id")
        result: dict[str, Any] = {"url": url}
        if session_id:
            result["providerSessionId"] = session_id
        return result

    async def create_customer_portal_session(self, params: PortalParams) -> dict:
        client = self._get_client()
        session = await client.customers.customer_portal.create(
            params.customer_id,
            return_url=params.return_url,
        )
        link = getattr(session, "link", None) or session.get("link")
        return {"url": link}

    async def handle_webhook(self, req: WebhookRequest) -> dict:
        client = self._get_client()
        webhook_key = self._config.get("webhook_key")
        try:
            event = await client.webhooks.unwrap(
                req.raw_body,
                headers=req.headers,
                key=webhook_key,
            )
        except Exception as exc:
            logger.error("Dodo webhook unwrap failed: %s", exc)
            is_transient = isinstance(exc, (ConnectionError, TimeoutError))
            if not is_transient:
                err_str = str(exc).lower()
                is_transient = any(
                    kw in err_str
                    for kw in ("timeout", "connection refused", "connection reset", "eof", "name resolution")
                )
            return {"received": False, "retryable": is_transient, "error": str(exc)}

        event_type = getattr(event, "type", None) or event.get("type", "")
        event_data = getattr(event, "data", None) or event.get("data", {})

        data_dict: dict[str, Any] = {}
        if hasattr(event_data, "model_dump"):
            data_dict = event_data.model_dump()
        elif isinstance(event_data, dict):
            data_dict = event_data
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
        if not user_id and self._resolve_user:
            user_id = await self._resolve_user(data_dict, metadata)

        await handle_dodo_billing_event(
            event_type,
            data_dict,
            user_id,
            metadata,
            self._sink,
            self._logger,
        )
        return {"received": True}

    async def cancel_subscription(self, subscription_id: str) -> None:
        client = self._get_client()
        await client.subscriptions.update(
            subscription_id,
            cancel_at_next_billing_date=True,
        )

    async def reactivate_subscription(self, subscription_id: str) -> None:
        client = self._get_client()
        await client.subscriptions.update(
            subscription_id,
            cancel_at_next_billing_date=False,
        )

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> dict:
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
        return {"url": url}

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> dict:
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
        return {"url": url}

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        client = self._get_client()
        response = await client.customers.retrieve_payment_methods(customer_id)
        items = getattr(response, "items", None) or response.get("items", [])
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
        return result

    async def create_customer(self, params: CreateCustomerParams) -> dict:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "email": params.email,
            "name": params.name,
        }
        if params.metadata:
            kwargs["metadata"] = params.metadata
        customer = await client.customers.create(**kwargs)
        cid = getattr(customer, "customer_id", None) or customer.get("customer_id")
        return {"customerId": cid}

    async def get_invoice_url(self, provider_payment_id: str) -> dict | None:
        client = self._get_client()
        payment = await client.payments.retrieve(provider_payment_id)
        link = getattr(payment, "payment_link", None) or payment.get("payment_link")
        if link:
            return {"url": link}
        return None

    async def change_plan(self, params: ChangePlanParams) -> None:
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
        await client.subscriptions.change_plan(params.provider_subscription_id, **kwargs)

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
        immediate_charge = getattr(response, "immediate_charge", None) or response.get("immediate_charge", {})
        line_items_raw = getattr(immediate_charge, "line_items", None) or immediate_charge.get("line_items", [])
        summary = getattr(immediate_charge, "summary", None) or immediate_charge.get("summary", {})
        line_items: list[ChangePlanLineItem] = []
        for item in line_items_raw:
            i = item.model_dump() if hasattr(item, "model_dump") else item
            if i.get("type") == "subscription":
                line_items.append(
                    ChangePlanLineItem(
                        product_id=str(i.get("product_id", "")),
                        name=str(i.get("name", i.get("description", ""))),
                        unit_price=int(i.get("unit_price", 0)),
                        quantity=int(i.get("quantity", 0)),
                        proration_factor=float(i.get("proration_factor", 0)),
                        currency=str(i.get("currency", "")),
                        tax=int(i.get("tax", 0)),
                        subtotal=0,
                    )
                )
        total = int(getattr(summary, "total_amount", None) or summary.get("total_amount", 0))
        settlement = int(getattr(summary, "settlement_amount", None) or summary.get("settlement_amount", 0))
        currency = str(getattr(summary, "settlement_currency", None) or summary.get("settlement_currency", "USD"))
        effective = str(getattr(immediate_charge, "effective_at", None) or immediate_charge.get("effective_at", ""))
        return ChangePlanPreview(
            total_amount=total,
            settlement_amount=settlement,
            currency=currency,
            line_items=line_items or None,
            effective_at=effective,
        )
