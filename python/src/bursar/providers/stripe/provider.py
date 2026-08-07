from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import stripe as stripe_mod

from bursar.bursar import BillingEventSink
from bursar.providers.stripe.event_mapper import handle_stripe_billing_event
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutPaymentStatus,
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
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    deduplicate_payment_methods,
    normalize_provider_logger,
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


def _scoped_idempotency_key(key: str | None, scope: str) -> str | None:
    if not key:
        return None
    suffix = f":{scope}"
    return f"{key[: 255 - len(suffix)]}{suffix}"


def _expandable_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    identifier = _stripe_val(value, "id")
    return str(identifier) if identifier else None


def _schedule_phase_params(phase: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in _stripe_val(phase, "items", []) or []:
        price_id = _expandable_id(_stripe_val(item, "price"))
        if not price_id:
            raise ValueError("Stripe subscription schedule item has no price")
        mapped: dict[str, Any] = {"price": price_id}
        quantity = _stripe_val(item, "quantity")
        if quantity is not None:
            mapped["quantity"] = quantity
        metadata = _stripe_val(item, "metadata")
        if metadata:
            mapped["metadata"] = metadata
        tax_rates = _stripe_val(item, "tax_rates")
        if tax_rates:
            mapped["tax_rates"] = [_expandable_id(rate) for rate in tax_rates]
        items.append(mapped)

    if not items:
        raise ValueError("Stripe subscription schedule phase has no billing items")

    result: dict[str, Any] = {
        "items": items,
        "start_date": _stripe_val(phase, "start_date"),
        "end_date": _stripe_val(phase, "end_date"),
    }
    automatic_tax = _stripe_val(phase, "automatic_tax")
    if automatic_tax:
        result["automatic_tax"] = {"enabled": bool(_stripe_val(automatic_tax, "enabled", False))}
    for field in (
        "billing_cycle_anchor",
        "collection_method",
        "currency",
        "description",
        "metadata",
        "proration_behavior",
        "trial_end",
    ):
        value = _stripe_val(phase, field)
        if value is not None:
            result[field] = value
    default_payment_method = _expandable_id(_stripe_val(phase, "default_payment_method"))
    if default_payment_method:
        result["default_payment_method"] = default_payment_method
    return result


def _stripe_proration_behavior(mode: str) -> str:
    return "none" if mode == "do_not_bill" else "always_invoice"


class StripeProvider(PaymentProvider):
    provider = "stripe"

    def __init__(
        self,
        *,
        event_sink: BillingEventSink,
        webhook_secret: str,
        get_client: Callable[[], Any] | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        if not webhook_secret.strip():
            raise ValueError("webhook_secret must not be empty")
        self._sink = event_sink
        self._webhook_secret = webhook_secret
        self._get_stripe = get_client or (lambda: stripe_mod)
        self._logger = normalize_provider_logger(logger)

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        if not params.user_id:
            raise ValueError("Authentication required for checkout")
        stripe = self._get_stripe()

        customer_id = params.customer_id
        if not customer_id:
            customer_kwargs: dict[str, Any] = {"metadata": {"userId": params.user_id}}
            if params.email:
                customer_kwargs["email"] = params.email
            customer_idempotency_key = _scoped_idempotency_key(params.idempotency_key, "customer")
            if customer_idempotency_key:
                customer_kwargs["idempotency_key"] = customer_idempotency_key
            customer = await stripe.Customer.create_async(**customer_kwargs)
            customer_id = customer["id"]

        quantity = params.quantity if params.quantity is not None else 1
        common: dict[str, Any] = {
            "customer": customer_id,
            "line_items": [{"price": params.product_id, "quantity": quantity}],
            "success_url": params.return_url,
            "cancel_url": params.cancel_url,
            "client_reference_id": params.user_id,
            "automatic_tax": {"enabled": True},
            "metadata": params.metadata or {},
        }
        if params.idempotency_key:
            common["idempotency_key"] = params.idempotency_key

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
        provider_session_id = _stripe_val(session, "id")
        if not provider_session_id:
            raise ValueError("Stripe checkout session returned no ID")
        return CheckoutSessionResult(
            url=str(url),
            customer_id=str(customer_id),
            provider_session_id=str(provider_session_id),
        )

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        stripe = self._get_stripe()
        session = await stripe.billing_portal.Session.create_async(
            customer=params.customer_id,
            return_url=params.return_url,
        )
        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe portal session returned no URL")
        return ProviderUrlResult(url=str(url))

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        stripe = self._get_stripe()
        session = await stripe.billing_portal.Session.create_async(
            customer=params.customer_id,
            return_url=params.return_url,
            flow_data={"type": "payment_method_update"},
        )
        url = _stripe_val(session, "url")
        if not url:
            raise ValueError("Stripe portal session returned no URL")
        return ProviderUrlResult(url=str(url))

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
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
        return ProviderUrlResult(url=str(url))

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        stripe = self._get_stripe()
        signature = req.headers.get("stripe-signature")
        if not signature:
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

        try:
            event = stripe.Webhook.construct_event(
                req.raw_body,
                signature,
                self._webhook_secret,
            )
        except stripe_mod.SignatureVerificationError:
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )
        except stripe_mod.APIError as e:
            self._logger.error("Stripe webhook temporarily unavailable", {"error": str(e)})
            return WebhookResult(
                received=False,
                retryable=True,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )
        except Exception as e:
            self._logger.warning("Stripe webhook verification failed", {"error": str(e)})
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

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
            getattr(event, "created", None),
        )
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=str(event.id) if event.id is not None else None,
            event_type=str(event.type) if event.type is not None else None,
        )

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        stripe = self._get_stripe()
        kwargs: dict[str, Any] = {"cancel_at_period_end": True}
        if idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
        await stripe.Subscription.modify_async(
            subscription_id,
            **kwargs,
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str | None = None) -> None:
        stripe = self._get_stripe()
        kwargs: dict[str, Any] = {"cancel_at_period_end": False}
        if idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
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
            kwargs["idempotency_key"] = idempotency_key
        await stripe.SubscriptionSchedule.release_async(provider_operation_id, **kwargs)

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        stripe = self._get_stripe()
        session = await stripe.checkout.Session.retrieve_async(provider_session_id, expand=["payment_intent"])
        if _stripe_val(session, "status") == "expired":
            return CheckoutSessionStatus(payment_status="cancelled")
        payment_status = _stripe_val(session, "payment_status")
        if payment_status in ("paid", "no_payment_required"):
            return CheckoutSessionStatus(payment_status="succeeded")
        if _stripe_val(session, "status") == "open":
            return CheckoutSessionStatus(payment_status="processing")
        payment_intent = _stripe_val(session, "payment_intent")
        raw_intent_status = _stripe_val(payment_intent, "status") if not isinstance(payment_intent, str) else None
        intent_status = raw_intent_status if isinstance(raw_intent_status, str) else None
        statuses: dict[str, CheckoutPaymentStatus] = {
            "succeeded": "succeeded",
            "processing": "processing",
            "requires_action": "requires_customer_action",
            "requires_payment_method": "requires_payment_method",
            "requires_confirmation": "requires_confirmation",
            "requires_capture": "requires_capture",
            "canceled": "cancelled",
        }
        mapped_status: CheckoutPaymentStatus = (
            statuses.get(intent_status, "processing") if intent_status else "processing"
        )
        return CheckoutSessionStatus(payment_status=mapped_status)

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        stripe = self._get_stripe()
        customer, methods = await asyncio.gather(
            stripe.Customer.retrieve_async(customer_id),
            stripe.PaymentMethod.list_async(customer=customer_id, type="card"),
        )
        if bool(_stripe_val(customer, "deleted", False)):
            return []
        default = _stripe_val(_stripe_val(customer, "invoice_settings", {}), "default_payment_method")
        default_id = _stripe_val(default, "id", default) if default else None
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
                    is_default=pm["id"] == default_id,
                )
            )
        return deduplicate_payment_methods(result)

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
            metadata={**(params.metadata or {}), "price_id": params.product_id},
            idempotency_key=params.idempotency_key,
        )
        raw_status = _stripe_val(intent, "status", "processing")
        status = {
            "succeeded": "succeeded",
            "processing": "processing",
            "requires_action": "requires_customer_action",
            "requires_payment_method": "requires_payment_method",
        }.get(raw_status, "failed")
        return SavedPaymentChargeResult.model_validate(
            {
                "provider_payment_id": _stripe_val(intent, "id"),
                "status": status,
                "amount_minor": _stripe_val(intent, "amount"),
                "currency": _stripe_val(intent, "currency"),
            }
        )

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        stripe = self._get_stripe()
        customer = await stripe.Customer.create_async(
            email=params.email,
            name=params.name,
            metadata=params.metadata or {},
        )
        return CreateCustomerResult(customer_id=str(customer["id"]))

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        stripe = self._get_stripe()
        invoice = await stripe.Invoice.retrieve_async(provider_payment_id)
        url = _stripe_val(invoice, "hosted_invoice_url")
        if not url:
            return None
        return ProviderUrlResult(url=str(url))

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult:
        stripe = self._get_stripe()
        subscription = await stripe.Subscription.retrieve_async(params.provider_subscription_id)
        items = _stripe_val(_stripe_val(subscription, "items", {}), "data", [])
        item = items[0] if items else None
        if not item:
            raise ValueError("Stripe subscription has no billing item")
        item_id = _stripe_val(item, "id")
        if params.effective_at == "next_billing_date":
            schedule_api: Any = stripe.SubscriptionSchedule
            create_kwargs: dict[str, Any] = {"from_subscription": params.provider_subscription_id}
            create_key = _scoped_idempotency_key(params.idempotency_key, "schedule-create")
            if create_key:
                create_kwargs["idempotency_key"] = create_key
            schedule = await schedule_api.create_async(**create_kwargs)
            phases = _stripe_val(schedule, "phases", [])
            current_phase = phases[0] if phases else None
            if current_phase is None:
                raise ValueError("Stripe subscription schedule has no current phase")
            update_kwargs: dict[str, Any] = {
                "phases": [
                    _schedule_phase_params(current_phase),
                    {
                        "items": [{"price": params.product_id, "quantity": params.quantity}],
                        "start_date": _stripe_val(current_phase, "end_date"),
                        "proration_behavior": "none",
                        **({"metadata": params.metadata} if params.metadata else {}),
                    },
                ],
                "proration_behavior": "none",
            }
            update_key = _scoped_idempotency_key(params.idempotency_key, "schedule-update")
            if update_key:
                update_kwargs["idempotency_key"] = update_key
            await schedule_api.modify_async(
                _stripe_val(schedule, "id"),
                **update_kwargs,
            )
            return ChangePlanResult(provider_operation_id=str(_stripe_val(schedule, "id")))
        kwargs: dict[str, Any] = {
            "items": [{"id": item_id, "price": params.product_id, "quantity": params.quantity}],
            "proration_behavior": _stripe_proration_behavior(params.proration_billing_mode),
            "payment_behavior": (
                "allow_incomplete" if params.on_payment_failure == "apply_change" else "pending_if_incomplete"
            ),
        }
        if params.metadata:
            kwargs["metadata"] = params.metadata
        update_key = _scoped_idempotency_key(params.idempotency_key, "subscription-update")
        if update_key:
            kwargs["idempotency_key"] = update_key
        updated = await stripe.Subscription.modify_async(params.provider_subscription_id, **kwargs)
        latest_invoice = _stripe_val(updated, "latest_invoice")
        return ChangePlanResult(provider_operation_id=str(latest_invoice) if latest_invoice else None)

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
                "proration_behavior": (
                    "none"
                    if params.effective_at == "next_billing_date"
                    else _stripe_proration_behavior(params.proration_billing_mode)
                ),
            },
        )
        total = int(_stripe_val(invoice, "total", 0) or 0)
        amount_due = int(_stripe_val(invoice, "amount_due", 0) or 0)
        currency = str(_stripe_val(invoice, "currency", "USD"))
        invoice_lines = _stripe_val(_stripe_val(invoice, "lines", {}), "data", []) or []
        price = await stripe.Price.retrieve_async(params.product_id)
        current_period_end = int(_stripe_val(item, "current_period_end", 0) or 0)
        next_billing_date = datetime.fromtimestamp(current_period_end, tz=UTC).isoformat()
        return ChangePlanPreview(
            total_amount=total,
            settlement_amount=amount_due,
            currency=currency,
            line_items=[
                ChangePlanLineItem(
                    product_id=params.product_id,
                    name=str(_stripe_val(line, "description", "Subscription change")),
                    unit_price=int(_stripe_val(line, "amount", 0) or 0),
                    quantity=int(_stripe_val(line, "quantity", 1) or 1),
                    proration_factor=1,
                    currency=str(_stripe_val(line, "currency", currency)),
                    tax=sum(int(_stripe_val(tax, "amount", 0) or 0) for tax in (_stripe_val(line, "taxes", []) or [])),
                    subtotal=int(_stripe_val(line, "amount", 0) or 0),
                )
                for line in invoice_lines
            ],
            effective_at=(
                next_billing_date if params.effective_at == "next_billing_date" else datetime.now(UTC).isoformat()
            ),
            recurring_amount=int(_stripe_val(price, "unit_amount", 0) or 0),
            recurring_currency=str(_stripe_val(price, "currency", currency)),
            next_billing_date=next_billing_date,
            tax_amount=sum(
                int(_stripe_val(tax, "amount", 0) or 0) for tax in (_stripe_val(invoice, "total_taxes", []) or [])
            ),
        )
