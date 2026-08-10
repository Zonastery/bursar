from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import stripe as stripe_mod

from bursar.bursar import BillingEventSink
from bursar.errors import ProviderResponseError
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
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderUrlResult,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    SavedPaymentChargeStatus,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    deduplicate_payment_methods,
    normalize_provider_logger,
)
from bursar.shared.idempotency import require_stable_key, scope_stable_key
from bursar.shared.numbers import MAX_SAFE_INTEGER

if TYPE_CHECKING:
    from stripe.params import CustomerCreateParams, SubscriptionScheduleUpdateParams, SubscriptionUpdateParams
    from stripe.params.checkout import SessionCreateParams


def _require_stripe_text(value: object, operation: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("stripe", operation, details={"field": field})
    return value


def _require_stripe_int(
    value: object,
    operation: str,
    field: str,
    *,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > MAX_SAFE_INTEGER:
        raise ProviderResponseError("stripe", operation, details={"field": field})
    return value


def _require_stripe_number(value: object, operation: str, field: str) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError as error:
            raise ProviderResponseError("stripe", operation, cause=error, details={"field": field}) from error
    else:
        raise ProviderResponseError("stripe", operation, details={"field": field})
    if not math.isfinite(parsed):
        raise ProviderResponseError("stripe", operation, details={"field": field})
    return parsed


def _request_options(idempotency_key: str) -> stripe_mod.RequestOptions:
    return {"idempotency_key": require_stable_key(idempotency_key)}


def _saved_payment_status(value: object) -> SavedPaymentChargeStatus:
    mapping: dict[str, SavedPaymentChargeStatus] = {
        "succeeded": "succeeded",
        "processing": "processing",
        "requires_action": "requires_customer_action",
        "requires_payment_method": "requires_payment_method",
        "requires_confirmation": "requires_confirmation",
        "requires_capture": "requires_capture",
        "canceled": "cancelled",
    }
    status = mapping.get(str(value))
    if status is None:
        raise ProviderResponseError("stripe", "charge_saved_payment_method", details={"field": "status"})
    return status


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


def _scoped_idempotency_key(key: str, scope: str) -> str:
    return scope_stable_key(key, scope)


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
            raise ProviderResponseError(
                "stripe",
                "change_plan",
                details={"field": "schedule.phases.items.price"},
            )
        mapped: dict[str, Any] = {"price": price_id}
        quantity = _stripe_val(item, "quantity")
        if quantity is not None:
            mapped["quantity"] = quantity
        metadata = _stripe_val(item, "metadata")
        if metadata:
            mapped["metadata"] = metadata
        tax_rates = _stripe_val(item, "tax_rates")
        if tax_rates:
            mapped["tax_rates"] = [
                _require_stripe_text(
                    _expandable_id(rate),
                    "change_plan",
                    "schedule.phases.items.tax_rates",
                )
                for rate in tax_rates
            ]
        items.append(mapped)

    if not items:
        raise ProviderResponseError(
            "stripe",
            "change_plan",
            details={"field": "schedule.phases.items"},
        )

    result: dict[str, Any] = {
        "items": items,
        "start_date": _stripe_val(phase, "start_date"),
        "end_date": _stripe_val(phase, "end_date"),
    }
    automatic_tax = _stripe_val(phase, "automatic_tax")
    if automatic_tax:
        enabled = _stripe_val(automatic_tax, "enabled")
        if type(enabled) is not bool:
            raise ProviderResponseError(
                "stripe",
                "change_plan",
                details={"field": "schedule.phases.automatic_tax.enabled"},
            )
        result["automatic_tax"] = {"enabled": enabled}
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


class StripeProvider:
    provider = "stripe"

    def __init__(
        self,
        *,
        event_sink: BillingEventSink,
        webhook_secret: str,
        get_client: Callable[[], stripe_mod.StripeClient],
        logger: ProviderLogger | None = None,
    ) -> None:
        if not webhook_secret.strip():
            raise ValueError("webhook_secret must not be empty")
        self._sink = event_sink
        self._webhook_secret = webhook_secret
        self._get_stripe = get_client
        self._logger = normalize_provider_logger(logger)

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        if not params.account_id:
            raise ValueError("Authentication required for checkout")
        stripe = self._get_stripe()

        customer_id = params.customer_id
        if not customer_id:
            customer_kwargs: dict[str, Any] = {"metadata": {"bursar_account_id": params.account_id}}
            if params.email:
                customer_kwargs["email"] = params.email
            customer_idempotency_key = _scoped_idempotency_key(params.idempotency_key, "customer")
            customer = await stripe.v1.customers.create_async(
                cast("CustomerCreateParams", customer_kwargs),
                _request_options(customer_idempotency_key),
            )
            customer_id = _require_stripe_text(customer.id, "create_checkout_session", "customer.id")

        quantity = params.quantity if params.quantity is not None else 1
        metadata = {**(params.metadata or {}), "bursar_account_id": params.account_id}
        common: dict[str, Any] = {
            "customer": customer_id,
            "line_items": [{"price": params.product_id, "quantity": quantity}],
            "success_url": params.return_url,
            "cancel_url": params.cancel_url,
            "client_reference_id": params.account_id,
            "automatic_tax": {"enabled": True},
            "metadata": metadata,
        }
        if params.type == "subscription":
            common.update(
                mode="subscription",
                subscription_data={
                    "metadata": metadata,
                },
            )
        else:
            common.update(
                mode="payment",
                payment_intent_data={
                    "metadata": metadata,
                },
            )
        session = await stripe.v1.checkout.sessions.create_async(
            cast("SessionCreateParams", common),
            _request_options(params.idempotency_key),
        )
        return CheckoutSessionResult(
            url=_require_stripe_text(session.url, "create_checkout_session", "session.url"),
            customer_id=_require_stripe_text(customer_id, "create_checkout_session", "customer_id"),
            provider_session_id=_require_stripe_text(session.id, "create_checkout_session", "session.id"),
        )

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        stripe = self._get_stripe()
        session = await stripe.v1.billing_portal.sessions.create_async(
            {"customer": params.customer_id, "return_url": params.return_url}
        )
        return ProviderUrlResult(url=_require_stripe_text(session.url, "create_customer_portal_session", "session.url"))

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        stripe = self._get_stripe()
        session = await stripe.v1.billing_portal.sessions.create_async(
            {
                "customer": params.customer_id,
                "return_url": params.return_url,
                "flow_data": {"type": "payment_method_update"},
            }
        )
        return ProviderUrlResult(
            url=_require_stripe_text(session.url, "create_update_payment_method_session", "session.url")
        )

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        stripe = self._get_stripe()
        session = await stripe.v1.checkout.sessions.create_async(
            {
                "customer": params.customer_id,
                "mode": "setup",
                "success_url": params.return_url,
                "cancel_url": params.cancel_url or params.return_url,
                "payment_method_types": ["card"],
            }
        )
        return ProviderUrlResult(
            url=_require_stripe_text(session.url, "create_payment_method_setup_session", "session.url")
        )

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
            event = stripe.construct_event(
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
        account_id = metadata.get("bursar_account_id")

        await handle_stripe_billing_event(
            event.type,
            event.id,
            data,
            account_id,
            metadata,
            self._sink,
            stripe,
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

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        stripe = self._get_stripe()
        await stripe.v1.subscriptions.update_async(
            subscription_id,
            {"cancel_at_period_end": True},
            _request_options(idempotency_key),
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        stripe = self._get_stripe()
        await stripe.v1.subscriptions.update_async(
            subscription_id,
            {"cancel_at_period_end": False},
            _request_options(idempotency_key),
        )

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        provider_operation_id: str | None = None,
        *,
        idempotency_key: str,
    ) -> None:
        if not provider_operation_id:
            raise ValueError("Stripe scheduled change has no schedule ID")
        stripe = self._get_stripe()
        await stripe.v1.subscription_schedules.release_async(
            provider_operation_id,
            {},
            _request_options(idempotency_key),
        )

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        stripe = self._get_stripe()
        try:
            session = await stripe.v1.checkout.sessions.retrieve_async(
                provider_session_id,
                {"expand": ["payment_intent"]},
            )
        except stripe_mod.InvalidRequestError as error:
            if error.code == "resource_missing":
                return None
            raise
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
        mapped_status = statuses.get(intent_status) if intent_status else None
        return CheckoutSessionStatus(payment_status=mapped_status)

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        stripe = self._get_stripe()
        customer, methods = await asyncio.gather(
            stripe.v1.customers.retrieve_async(customer_id),
            stripe.v1.customers.payment_methods.list_async(customer_id, {"type": "card"}),
        )
        if bool(_stripe_val(customer, "deleted", False)):
            return []
        default = _stripe_val(_stripe_val(customer, "invoice_settings", {}), "default_payment_method")
        default_id = _stripe_val(default, "id", default) if default else None
        result: list[PaymentMethodInfo] = []
        for payment_method in methods.data:
            card = payment_method.card
            if card is None:
                raise ProviderResponseError("stripe", "list_payment_methods", details={"field": "card"})
            last4 = _require_stripe_text(card.last4, "list_payment_methods", "card.last4")
            if len(last4) != 4 or not last4.isascii() or not last4.isdigit():
                raise ProviderResponseError(
                    "stripe",
                    "list_payment_methods",
                    details={"field": "card.last4"},
                )
            payment_method_id = _require_stripe_text(payment_method.id, "list_payment_methods", "id")
            result.append(
                PaymentMethodInfo(
                    id=payment_method_id,
                    last4=last4,
                    brand=_require_stripe_text(card.brand, "list_payment_methods", "card.brand"),
                    expiry_month=_require_stripe_int(
                        card.exp_month,
                        "list_payment_methods",
                        "card.exp_month",
                        minimum=1,
                    ),
                    expiry_year=_require_stripe_int(
                        card.exp_year,
                        "list_payment_methods",
                        "card.exp_year",
                        minimum=1,
                    ),
                    is_default=payment_method_id == default_id,
                )
            )
        return deduplicate_payment_methods(result)

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        price = await self._get_stripe().v1.prices.retrieve_async(params.product_id)
        unit_amount = price.unit_amount
        if unit_amount is None:
            raise ProviderResponseError(
                "stripe",
                "preview_saved_payment_charge",
                details={"field": "unit_amount"},
            )
        return SavedPaymentChargeQuote(
            amount_minor=_require_stripe_int(
                unit_amount * params.quantity,
                "preview_saved_payment_charge",
                "amount",
            ),
            currency=_require_stripe_text(
                price.currency,
                "preview_saved_payment_charge",
                "currency",
            ).upper(),
        )

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        stripe = self._get_stripe()
        price = await stripe.v1.prices.retrieve_async(params.product_id)
        unit_amount = price.unit_amount
        if unit_amount is None:
            raise ProviderResponseError(
                "stripe",
                "charge_saved_payment_method",
                details={"field": "unit_amount"},
            )
        intent = await stripe.v1.payment_intents.create_async(
            {
                "amount": unit_amount * params.quantity,
                "currency": price.currency,
                "customer": params.customer_id,
                "payment_method": params.payment_method_id,
                "confirm": True,
                "off_session": True,
                "metadata": {**params.metadata, "price_id": params.product_id},
            },
            _request_options(params.idempotency_key),
        )
        return SavedPaymentChargeResult(
            provider_payment_id=_require_stripe_text(
                intent.id,
                "charge_saved_payment_method",
                "id",
            ),
            status=_saved_payment_status(intent.status),
            amount_minor=_require_stripe_int(
                intent.amount,
                "charge_saved_payment_method",
                "amount",
            ),
            currency=_require_stripe_text(
                intent.currency,
                "charge_saved_payment_method",
                "currency",
            ),
        )

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        stripe = self._get_stripe()
        customer = await stripe.v1.customers.create_async(
            {"email": params.email, "name": params.name, "metadata": params.metadata},
            _request_options(params.idempotency_key),
        )
        return CreateCustomerResult(customer_id=_require_stripe_text(customer.id, "create_customer", "customer.id"))

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        stripe = self._get_stripe()
        invoice = await stripe.v1.invoices.retrieve_async(provider_payment_id)
        if invoice.hosted_invoice_url is None:
            return None
        return ProviderUrlResult(
            url=_require_stripe_text(invoice.hosted_invoice_url, "get_invoice_url", "hosted_invoice_url")
        )

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult:
        stripe = self._get_stripe()
        subscription = await stripe.v1.subscriptions.retrieve_async(params.provider_subscription_id)
        item = subscription.items.data[0] if subscription.items.data else None
        if item is None:
            raise ProviderResponseError(
                "stripe",
                "change_plan",
                details={"field": "subscription.items"},
            )
        item_id = _require_stripe_text(item.id, "change_plan", "subscription.items.id")
        if params.effective_at == "next_billing_date":
            create_key = _scoped_idempotency_key(params.idempotency_key, "schedule-create")
            schedule = await stripe.v1.subscription_schedules.create_async(
                {"from_subscription": params.provider_subscription_id},
                _request_options(create_key),
            )
            current_phase = schedule.phases[0] if schedule.phases else None
            if current_phase is None:
                raise ProviderResponseError(
                    "stripe",
                    "change_plan",
                    details={"field": "schedule.phases"},
                )
            update_kwargs: dict[str, Any] = {
                "phases": [
                    _schedule_phase_params(current_phase),
                    {
                        "items": [{"price": params.product_id, "quantity": params.quantity}],
                        "start_date": current_phase.end_date,
                        "proration_behavior": "none",
                        **({"metadata": params.metadata} if params.metadata else {}),
                    },
                ],
                "proration_behavior": "none",
            }
            update_key = _scoped_idempotency_key(params.idempotency_key, "schedule-update")
            schedule_id = _require_stripe_text(schedule.id, "change_plan", "schedule.id")
            await stripe.v1.subscription_schedules.update_async(
                schedule_id,
                cast("SubscriptionScheduleUpdateParams", update_kwargs),
                _request_options(update_key),
            )
            return ChangePlanResult(provider_operation_id=schedule_id)
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
        updated = await stripe.v1.subscriptions.update_async(
            params.provider_subscription_id,
            cast("SubscriptionUpdateParams", kwargs),
            _request_options(update_key),
        )
        return ChangePlanResult(provider_operation_id=_expandable_id(updated.latest_invoice))

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        stripe = self._get_stripe()
        subscription = await stripe.v1.subscriptions.retrieve_async(params.provider_subscription_id)
        item = subscription.items.data[0] if subscription.items.data else None
        if item is None:
            raise ProviderResponseError(
                "stripe",
                "preview_change_plan",
                details={"field": "subscription.items"},
            )
        item_id = _require_stripe_text(item.id, "preview_change_plan", "subscription.items.id")
        customer_id = _require_stripe_text(
            _expandable_id(subscription.customer),
            "preview_change_plan",
            "subscription.customer",
        )
        invoice = await stripe.v1.invoices.create_preview_async(
            {
                "customer": customer_id,
                "subscription": params.provider_subscription_id,
                "subscription_details": {
                    "items": [{"id": item_id, "price": params.product_id, "quantity": params.quantity}],
                    "proration_behavior": (
                        "none"
                        if params.effective_at == "next_billing_date"
                        else _stripe_proration_behavior(params.proration_billing_mode)
                    ),
                },
            }
        )
        total = _require_stripe_int(invoice.total, "preview_change_plan", "invoice.total")
        amount_due = _require_stripe_int(invoice.amount_due, "preview_change_plan", "invoice.amount_due")
        currency = _require_stripe_text(invoice.currency, "preview_change_plan", "invoice.currency")
        price = await stripe.v1.prices.retrieve_async(params.product_id)
        current_period_end = _require_stripe_int(
            item.current_period_end,
            "preview_change_plan",
            "subscription.items.current_period_end",
            minimum=1,
        )
        next_billing_date = datetime.fromtimestamp(current_period_end, tz=UTC).isoformat()
        line_items: list[ChangePlanLineItem] = []
        for line in invoice.lines.data:
            parent = line.parent
            if parent is None or parent.subscription_item_details is None:
                continue
            pricing = line.pricing
            price_details = pricing.price_details if pricing is not None else None
            if pricing is None or price_details is None:
                raise ProviderResponseError(
                    "stripe",
                    "preview_change_plan",
                    details={"field": "invoice.lines.pricing.price_details"},
                )
            quantity = (
                1
                if line.quantity is None
                else _require_stripe_int(
                    line.quantity,
                    "preview_change_plan",
                    "invoice.lines.quantity",
                    minimum=1,
                )
            )
            unit_price = _require_stripe_number(
                pricing.unit_amount_decimal,
                "preview_change_plan",
                "invoice.lines.pricing.unit_amount_decimal",
            )
            subtotal = _require_stripe_int(
                line.subtotal,
                "preview_change_plan",
                "invoice.lines.subtotal",
                minimum=-(2**63),
            )
            expected_subtotal = unit_price * quantity
            taxes = line.taxes or []
            line_items.append(
                ChangePlanLineItem(
                    product_id=_require_stripe_text(
                        _expandable_id(price_details.price),
                        "preview_change_plan",
                        "invoice.lines.pricing.price_details.price",
                    ),
                    name=_require_stripe_text(
                        line.description,
                        "preview_change_plan",
                        "invoice.lines.description",
                    ),
                    unit_price=unit_price,
                    quantity=quantity,
                    proration_factor=(1 if expected_subtotal == 0 else subtotal / expected_subtotal),
                    currency=_require_stripe_text(
                        line.currency,
                        "preview_change_plan",
                        "invoice.lines.currency",
                    ),
                    tax=sum(
                        _require_stripe_int(
                            tax.amount,
                            "preview_change_plan",
                            "invoice.lines.taxes.amount",
                        )
                        for tax in taxes
                    ),
                    subtotal=subtotal,
                )
            )
        invoice_created = _require_stripe_int(
            invoice.created,
            "preview_change_plan",
            "invoice.created",
            minimum=1,
        )
        return ChangePlanPreview(
            total_amount=total,
            settlement_amount=amount_due,
            currency=currency,
            line_items=line_items,
            effective_at=(
                next_billing_date
                if params.effective_at == "next_billing_date"
                else datetime.fromtimestamp(invoice_created, tz=UTC).isoformat()
            ),
            recurring_amount=price.unit_amount,
            recurring_currency=_require_stripe_text(
                price.currency,
                "preview_change_plan",
                "price.currency",
            ),
            next_billing_date=next_billing_date,
            tax_amount=sum(
                _require_stripe_int(
                    tax.amount,
                    "preview_change_plan",
                    "invoice.total_taxes.amount",
                )
                for tax in (invoice.total_taxes or [])
            ),
        )
