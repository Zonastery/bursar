from __future__ import annotations

from collections.abc import Callable
from typing import Any

import stripe as stripe_mod

from bursar.billing.manager import BillingManager
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event
from bursar.providers.types import (
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    ProviderLogger,
    UpdatePaymentMethodParams,
    WebhookRequest,
)


def _stripe_val(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return {k: _stripe_val(obj, k) for k in dir(obj) if not k.startswith("_")}


class StripeProvider(PaymentProvider):
    provider = "stripe"

    def __init__(
        self,
        bm: BillingManager,
        webhook_secret: str = "",
        get_stripe: Callable[[], Any] | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        self._bm = bm
        self._webhook_secret = webhook_secret
        self._get_stripe = get_stripe or (lambda: stripe_mod)
        self._logger = logger

    async def create_checkout_session(self, params: CheckoutParams) -> dict:
        if not params.user_id:
            raise ValueError("Authentication required for checkout")
        stripe = self._get_stripe()

        customer_id = params.customer_id
        if not customer_id:
            customer = await stripe.Customer.create_async(
                metadata={"userId": params.user_id},
            )
            customer_id = customer["id"]

        common = {
            "customer": customer_id,
            "line_items": [{"price": params.product_id, "quantity": params.quantity or 1}],
            "success_url": params.return_url,
            "cancel_url": params.cancel_url,
            "client_reference_id": params.user_id,
            "automatic_tax": {"enabled": True},
            "metadata": params.metadata or {},
        }

        if params.type == "subscription":
            session = await stripe.checkout.Session.create_async(
                mode="subscription",
                subscription_data={
                    "metadata": {"userId": params.user_id, **(params.metadata or {})},
                },
                **common,
            )
        else:
            session = await stripe.checkout.Session.create_async(
                mode="payment",
                payment_intent_data={
                    "metadata": {"userId": params.user_id, **(params.metadata or {})},
                },
                **common,
            )

        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe checkout session returned no URL")
        return {"url": url, "customerId": customer_id}

    async def create_customer_portal_session(self, params: PortalParams) -> dict:
        stripe = self._get_stripe()
        session = await stripe.billing_portal.Session.create_async(
            customer=params.customer_id,
            return_url=params.return_url,
        )
        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe portal session returned no URL")
        return {"url": url}

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> dict:
        stripe = self._get_stripe()
        session = await stripe.billing_portal.Session.create_async(
            customer=params.customer_id,
            return_url=params.return_url,
            flow_data={"type": "payment_method_update"},
        )
        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe portal session returned no URL")
        return {"url": url}

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> dict:
        stripe = self._get_stripe()
        session = await stripe.checkout.Session.create_async(
            customer=params.customer_id,
            mode="setup",
            success_url=params.return_url,
            cancel_url=params.cancel_url or params.return_url,
            payment_method_types=["card"],
        )
        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe setup session returned no URL")
        return {"url": url}

    async def handle_webhook(self, req: WebhookRequest) -> dict:
        stripe = self._get_stripe()
        signature = req.headers.get("stripe-signature")
        if not signature:
            return {"received": False, "retryable": False}

        try:
            event = stripe.Webhook.construct_event(
                req.raw_body,
                signature,
                self._webhook_secret,
            )
        except stripe_mod.error.SignatureVerificationError:  # type: ignore[attr-defined]
            return {"received": False, "retryable": False}
        except Exception:
            return {"received": False, "retryable": True}

        data = event.data.object
        data_dict = _stripe_dict(data)
        user_id = data_dict.get("client_reference_id")
        md = data_dict.get("metadata", {}) or {}
        metadata = {str(k): str(v) for k, v in md.items()}
        if not user_id:
            user_id = metadata.get("userId")

        await handle_stripe_billing_event(
            event.type,
            event.id,
            data,
            user_id,
            metadata,
            self._bm,
            self._get_stripe(),
            self._logger,
        )
        return {"received": True}

    async def cancel_subscription(self, subscription_id: str) -> None:
        stripe = self._get_stripe()
        await stripe.Subscription.modify_async(
            subscription_id,
            cancel_at_period_end=True,
        )

    async def reactivate_subscription(self, subscription_id: str) -> None:
        stripe = self._get_stripe()
        await stripe.Subscription.modify_async(
            subscription_id,
            cancel_at_period_end=False,
        )

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        stripe = self._get_stripe()
        methods = await stripe.PaymentMethod.list_async(
            customer=customer_id,
            type="card",
        )
        result: list[PaymentMethodInfo] = []
        for pm in _stripe_val(methods, "data", []):
            card = _stripe_val(pm, "card", {}) or {}
            result.append(
                PaymentMethodInfo(
                    id=pm["id"],
                    last4=_stripe_val(card, "last4", ""),
                    brand=_stripe_val(card, "brand", "unknown"),
                    expiry_month=_stripe_val(card, "exp_month", 0),
                    expiry_year=_stripe_val(card, "exp_year", 0),
                )
            )
        return result

    async def create_customer(self, params: CreateCustomerParams) -> dict:
        stripe = self._get_stripe()
        customer = await stripe.Customer.create_async(
            email=params.email,
            name=params.name,
            metadata=params.metadata or {},
        )
        return {"customerId": customer["id"]}

    async def get_invoice_url(self, provider_payment_id: str) -> dict | None:
        stripe = self._get_stripe()
        invoice = await stripe.Invoice.retrieve_async(provider_payment_id)
        url = _stripe_val(invoice, "hosted_invoice_url")
        if not url:
            return None
        return {"url": url}
