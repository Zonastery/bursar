from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from bursar.bursar import BillingEventSink
from bursar.providers.dodo.event_mapper import handle_dodo_billing_event
from bursar.providers.types import (
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
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
        if params.metadata:
            session_kwargs["metadata"] = params.metadata
        if params.customer_id:
            session_kwargs["customer"] = {"customer_id": params.customer_id}

        session = await client.checkout_sessions.create(**session_kwargs)
        url = getattr(session, "checkout_url", None) or session.get("checkout_url")
        if not url:
            raise ValueError("Checkout session returned no URL")
        return {"url": url}

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
        response = await client.customers.wallets.list(customer_id)
        items = getattr(response, "items", None) or response.get("items", [])
        result: list[PaymentMethodInfo] = []
        for w in items:
            w_dict = w.model_dump() if hasattr(w, "model_dump") else w
            result.append(
                PaymentMethodInfo(
                    id=str(w_dict.get("payment_method_id", w_dict.get("id", ""))),
                    last4=str(w_dict.get("last4", "")),
                    brand=str(w_dict.get("brand", "unknown")),
                    expiry_month=int(w_dict.get("exp_month", w_dict.get("expiry_month", 0))),
                    expiry_year=int(w_dict.get("exp_year", w_dict.get("expiry_year", 0))),
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
