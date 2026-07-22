from __future__ import annotations

from collections.abc import Callable
from typing import Any

import stripe as stripe_mod

from bursar.bursar import BillingEventSink
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event
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
    ProviderLogger,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
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
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return {k: _stripe_val(obj, k) for k in dir(obj) if not k.startswith("_")}


class StripeProvider(PaymentProvider):
    provider = "stripe"

    def __init__(
        self,
        sink: BillingEventSink,
        webhook_secret: str = "",
        get_stripe: Callable[[], Any] | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        self._sink = sink
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

        quantity = params.quantity if params.quantity is not None else 1
        common = {
            "customer": customer_id,
            "line_items": [{"price": params.product_id, "quantity": quantity}],
            "success_url": params.return_url,
            "cancel_url": params.cancel_url,
            "client_reference_id": params.user_id,
            "automatic_tax": {"enabled": True},
            "metadata": params.metadata or {},
        }
        if params.idempotency_key:
            common["options"] = {"idempotency_key": params.idempotency_key}

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
        except stripe_mod.error.APIError as e:  # type: ignore[attr-defined]
            return {"received": False, "retryable": True, "error": str(e)}
        except Exception as e:
            return {"received": False, "retryable": False, "error": str(e)}

        data = event.data.object
        data_dict = _stripe_dict(data)
        md = data_dict.get("metadata", {}) or {}
        metadata = {str(k): str(v) for k, v in md.items()}
        user_id = metadata.get("userId")

        await handle_stripe_billing_event(
            event.type,
            event.id,
            data,
            user_id,
            metadata,
            self._sink,
            self._get_stripe(),
            self._logger,
        )
        return {"received": True}

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        stripe = self._get_stripe()
        kwargs: dict[str, Any] = {"cancel_at_period_end": True}
        if idempotency_key:
            kwargs["options"] = {"idempotency_key": idempotency_key}
        await stripe.Subscription.modify_async(
            subscription_id,
            **kwargs,
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        stripe = self._get_stripe()
        kwargs: dict[str, Any] = {"cancel_at_period_end": False}
        if idempotency_key:
            kwargs["options"] = {"idempotency_key": idempotency_key}
        await stripe.Subscription.modify_async(
            subscription_id,
            **kwargs,
        )

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if not provider_operation_id:
            raise ValueError("Stripe scheduled change has no schedule ID")
        stripe = self._get_stripe()
        kwargs: dict[str, Any] = {}
        if idempotency_key:
            kwargs["options"] = {"idempotency_key": idempotency_key}
        await stripe.SubscriptionSchedule.release_async(provider_operation_id, **kwargs)

    async def get_checkout_session_status(self, provider_session_id: str) -> dict | None:
        stripe = self._get_stripe()
        session = await stripe.checkout.Session.retrieve_async(provider_session_id)
        if _stripe_val(session, "status") == "expired":
            return {"paymentStatus": "cancelled"}
        payment_status = _stripe_val(session, "payment_status")
        if payment_status in ("paid", "no_payment_required"):
            return {"paymentStatus": "succeeded"}
        if payment_status == "unpaid":
            return {"paymentStatus": "failed"}
        return {"paymentStatus": None}

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

    async def get_default_payment_method(self, customer_id: str) -> PaymentMethodInfo | None:
        customer = await self._get_stripe().Customer.retrieve_async(customer_id)
        default = _stripe_val(_stripe_val(customer, "invoice_settings", {}), "default_payment_method")
        default_id = _stripe_val(default, "id", default) if default else None
        if not default_id:
            return None
        return next(
            (method for method in await self.list_payment_methods(customer_id) if method.id == default_id), None
        )

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        price = await self._get_stripe().Price.retrieve_async(params.product_id)
        unit_amount = _stripe_val(price, "unit_amount")
        if unit_amount is None:
            raise ValueError("Stripe top-up price has no fixed amount")
        return SavedPaymentChargeQuote(
            amount_minor=int(unit_amount) * params.quantity,
            currency=str(_stripe_val(price, "currency", "USD")).upper(),
        )

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        stripe = self._get_stripe()
        price = await stripe.Price.retrieve_async(params.product_id)
        unit_amount = _stripe_val(price, "unit_amount")
        if unit_amount is None:
            raise ValueError("Stripe top-up price has no fixed amount")
        intent = await stripe.PaymentIntent.create_async(
            amount=int(unit_amount) * params.quantity,
            currency=_stripe_val(price, "currency"),
            customer=params.customer_id,
            payment_method=params.payment_method_id,
            confirm=True,
            off_session=True,
            metadata=params.metadata or {},
            idempotency_key=params.idempotency_key,
        )
        raw_status = _stripe_val(intent, "status", "processing")
        status = {
            "succeeded": "succeeded",
            "processing": "processing",
            "requires_action": "requires_customer_action",
            "requires_payment_method": "requires_payment_method",
        }.get(raw_status, "failed")
        return SavedPaymentChargeResult(
            provider_payment_id=_stripe_val(intent, "id"),
            status=status,
            amount_minor=_stripe_val(intent, "amount"),
            currency=_stripe_val(intent, "currency"),
        )

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

    async def change_plan(self, params: ChangePlanParams) -> None:
        stripe = self._get_stripe()
        subscription = await stripe.Subscription.retrieve_async(params.provider_subscription_id)
        items = _stripe_val(_stripe_val(subscription, "items", {}), "data", [])
        item = items[0] if items else None
        if not item:
            raise ValueError("Stripe subscription has no billing item")
        item_id = _stripe_val(item, "id")
        options = {"idempotency_key": params.idempotency_key} if params.idempotency_key else None
        if params.effective_at == "next_billing_date":
            schedule = await stripe.SubscriptionSchedule.create_async(
                from_subscription=params.provider_subscription_id,
                options=options,
            )
            phases = _stripe_val(schedule, "phases", [])
            if len(phases) < 2:
                raise ValueError("Stripe subscription schedule has no next phase")
            phases[1]["items"] = [{"price": params.product_id, "quantity": params.quantity}]
            await stripe.SubscriptionSchedule.modify_async(
                _stripe_val(schedule, "id"),
                phases=phases,
                options=options,
            )
            return
        kwargs: dict[str, Any] = {
            "items": [{"id": item_id, "price": params.product_id, "quantity": params.quantity}],
            "proration_behavior": "always_invoice",
            "payment_behavior": "pending_if_incomplete",
        }
        if options:
            kwargs["options"] = options
        await stripe.Subscription.modify_async(params.provider_subscription_id, **kwargs)

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        stripe = self._get_stripe()
        subscription = await stripe.Subscription.retrieve_async(params.provider_subscription_id)
        items = _stripe_val(_stripe_val(subscription, "items", {}), "data", [])
        item = items[0] if items else None
        if not item:
            raise ValueError("Stripe subscription has no billing item")
        customer = _stripe_val(subscription, "customer")
        invoice = await stripe.Invoice.create_preview_async(
            customer=customer,
            subscription=params.provider_subscription_id,
            subscription_details={
                "items": [{"id": _stripe_val(item, "id"), "price": params.product_id, "quantity": params.quantity}],
                "proration_behavior": "none" if params.effective_at == "next_billing_date" else "always_invoice",
            },
        )
        total = int(_stripe_val(invoice, "total", 0) or 0)
        subtotal = int(_stripe_val(invoice, "subtotal", total) or 0)
        tax = (
            int(_stripe_val(invoice, "total_taxes", [{}])[0].get("amount", 0))
            if _stripe_val(invoice, "total_taxes", [])
            else 0
        )
        return ChangePlanPreview(
            total_amount=total,
            settlement_amount=total,
            currency=str(_stripe_val(invoice, "currency", "USD")).upper(),
            line_items=[
                ChangePlanLineItem(
                    product_id=params.product_id,
                    quantity=params.quantity,
                    subtotal=subtotal,
                    tax=tax,
                    currency=str(_stripe_val(invoice, "currency", "USD")).upper(),
                )
            ],
            effective_at=params.effective_at or "immediately",
            tax_amount=tax,
        )
