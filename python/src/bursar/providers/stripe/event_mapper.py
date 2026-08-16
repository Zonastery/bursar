from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from bursar.billing.types import (
    BillingCustomerInfo,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventType,
    BillingInvoiceInfo,
    BillingPaymentInfo,
    BillingRefundInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.bursar import BillingEventSink
from bursar.providers._shared import (
    call_billing_event_sink,
    parse_subscription_status,
    require_currency,
    require_minor_units,
    require_provider_string,
)
from bursar.providers.types import ProviderLogger, StdlibProviderLogger
from bursar.shared.diagnostics import persisted_diagnostic_summary

if TYPE_CHECKING:
    from stripe import StripeClient

STRIPE_CHECKOUT_EXPAND = ("line_items",)

_log = StdlibProviderLogger(logging.getLogger(__name__))

StripeEventHandler = Callable[
    [str, Any, str | None, dict[str, str], BillingEventSink, "StripeClient", ProviderLogger, str],
    Awaitable[None],
]


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(obj, key, default)


def _timestamp(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _expandable_id(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    identifier = _value(raw, "id")
    return str(identifier) if identifier else None


def _metadata(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    items = raw.items() if hasattr(raw, "items") else []
    return {str(key): str(value) for key, value in items}


def _subscription_period_value(sub: Any, field: str) -> int | float | None:
    items = _value(_value(sub, "items", {}), "data", []) or []
    return _value(items[0], field) if items else None


def _build_end(sub: Any) -> str | None:
    return _timestamp(_subscription_period_value(sub, "current_period_end"))


def _build_start(sub: Any) -> str | None:
    return _timestamp(_subscription_period_value(sub, "current_period_start"))


def _build_end_from_invoice(invoice: Any) -> str | None:
    return _timestamp(_value(invoice, "period_end"))


def _build_start_from_invoice(invoice: Any) -> str | None:
    return _timestamp(_value(invoice, "period_start"))


def _subscription_refs(sub: Any) -> ProviderRef | None:
    items = _value(_value(sub, "items", {}), "data", []) or []
    price = _value(items[0], "price", {}) if items else {}
    price_id = _expandable_id(price)
    product_id = _expandable_id(_value(price, "product"))
    if not price_id and not product_id:
        return None
    return ProviderRef(price_id=price_id, product_id=product_id)


def _subscription_info(sub: Any, refs: ProviderRef | None = None) -> BillingSubscriptionInfo:
    return BillingSubscriptionInfo(
        provider_subscription_id=require_provider_string(_value(sub, "id"), "Stripe subscription.id"),
        status=parse_subscription_status(_value(sub, "status")),
        cancel_at_period_end=_value(sub, "cancel_at_period_end"),
        period_start=_build_start(sub),
        period_end=_build_end(sub),
        trial_end=_timestamp(_value(sub, "trial_end")),
        cancel_at=_timestamp(_value(sub, "cancel_at")),
        ended_at=_timestamp(_value(sub, "ended_at")),
        refs=refs if refs is not None else _subscription_refs(sub),
    )


def _customer_info(session: Any) -> BillingCustomerInfo | None:
    raw_customer = _value(session, "customer")
    customer_id = _expandable_id(raw_customer)
    customer_email = None if isinstance(raw_customer, str) else _value(raw_customer, "email")
    customer_details = _value(session, "customer_details", {}) or {}
    email = customer_email or _value(customer_details, "email")
    if not customer_id and not email:
        return None
    return BillingCustomerInfo(
        provider_customer_id=customer_id,
        email=str(email) if email else None,
    )


def _checkout_payment_info(
    session: Any,
    expanded: Any,
    status: Literal["succeeded", "failed"],
) -> BillingPaymentInfo:
    line_items = _value(expanded, "line_items", {}) or {}
    line_data = _value(line_items, "data", []) or []
    price = _value(line_data[0], "price", {}) if line_data else {}
    price_id = _expandable_id(price)
    product_id = _expandable_id(_value(price, "product"))
    refs = ProviderRef(product_id=product_id, price_id=price_id) if product_id or price_id else None
    total_details = _value(session, "total_details", {}) or {}
    tax_minor = require_minor_units(
        _value(total_details, "amount_tax", 0) or 0,
        "Stripe checkout session.total_details.amount_tax",
    )
    subtotal = _value(session, "amount_subtotal")
    if subtotal is None:
        subtotal = max(0, int(_value(session, "amount_total", 0) or 0) - tax_minor)
    payment_intent_id = _expandable_id(_value(session, "payment_intent"))
    provider_payment_id = payment_intent_id or _value(session, "id")
    return BillingPaymentInfo(
        provider_payment_id=require_provider_string(
            provider_payment_id,
            "Stripe checkout session payment identifier",
        ),
        amount_minor=require_minor_units(subtotal, "Stripe checkout session.amount_subtotal"),
        tax_minor=tax_minor,
        currency=require_currency(_value(session, "currency"), "Stripe checkout session.currency"),
        purpose="subscription" if _value(session, "mode") == "subscription" else "credit_topup",
        status=status,
        refs=refs,
    )


def _invoice_subscription_id(invoice: Any) -> str | None:
    parent = _value(invoice, "parent", {}) or {}
    details = _value(parent, "subscription_details", {}) or {}
    current = _expandable_id(_value(details, "subscription"))
    return current or _expandable_id(_value(invoice, "subscription"))


def _invoice_metadata(invoice: Any) -> dict[str, str]:
    parent = _value(invoice, "parent", {}) or {}
    details = _value(parent, "subscription_details", {}) or {}
    return {
        **_metadata(_value(details, "metadata")),
        **_metadata(_value(invoice, "metadata")),
    }


def _invoice_payment_id(invoice: Any) -> str:
    payments = _value(_value(invoice, "payments", {}), "data", []) or []
    for invoice_payment in payments:
        payment = _value(invoice_payment, "payment", {}) or {}
        payment_intent_id = _expandable_id(_value(payment, "payment_intent"))
        if payment_intent_id:
            return payment_intent_id
    return require_provider_string(_value(invoice, "id"), "Stripe invoice.id")


def _invoice_tax_minor(invoice: Any) -> int:
    taxes = _value(invoice, "total_taxes", []) or []
    return require_minor_units(
        sum(int(_value(tax, "amount", 0) or 0) for tax in taxes),
        "Stripe invoice.total_taxes",
    )


def _stripe_dispute_status(value: object) -> Literal["needs_response", "under_review", "won", "lost", "closed"]:
    status = require_provider_string(value, "Stripe dispute.status")
    statuses: dict[str, Literal["needs_response", "under_review", "won", "lost", "closed"]] = {
        "needs_response": "needs_response",
        "warning_needs_response": "needs_response",
        "under_review": "under_review",
        "warning_under_review": "under_review",
        "won": "won",
        "prevented": "won",
        "lost": "lost",
        "warning_closed": "closed",
    }
    normalized = statuses.get(status)
    if normalized is None:
        raise ValueError(f"Unsupported Stripe dispute status: {status}")
    return normalized


async def _handle_checkout_event(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    outcome: Literal["completed", "succeeded", "failed"],
) -> None:
    session = data
    if outcome == "completed" and _value(session, "payment_status") == "unpaid":
        logger.debug(
            "Stripe Checkout completed with a delayed payment",
            {"sessionId": _value(session, "id")},
        )
        return

    expanded = await stripe.v1.checkout.sessions.retrieve_async(
        _value(session, "id"),
        {"expand": list(STRIPE_CHECKOUT_EXPAND)},
    )
    metadata = {**event_metadata, **_metadata(_value(session, "metadata"))}
    uid = account_id or _value(session, "client_reference_id") or metadata.get("bursar_account_id")
    customer = _customer_info(session)
    subscription_id = _expandable_id(_value(session, "subscription"))

    if outcome == "failed":
        subscription = None
        if subscription_id:
            subscription = _subscription_info(await stripe.v1.subscriptions.retrieve_async(subscription_id))
        call_billing_event_sink(
            sink,
            BillingEvent(
                provider="stripe",
                event_id=event_id,
                event_type=BillingEventType.payment_failed,
                occurred_at=occurred_at,
                account_id=uid,
                customer=customer,
                subscription=subscription,
                payment=_checkout_payment_info(session, expanded, "failed"),
                metadata=metadata,
            ),
        )
        return

    if _value(session, "mode") == "subscription" and subscription_id:
        sub = await stripe.v1.subscriptions.retrieve_async(subscription_id)
        plan_slug = metadata.get("plan_slug")
        refs = ProviderRef(lookup_key=plan_slug) if plan_slug else _subscription_refs(sub)
        call_billing_event_sink(
            sink,
            BillingEvent(
                provider="stripe",
                event_id=event_id,
                event_type=BillingEventType.checkout_completed,
                occurred_at=occurred_at,
                account_id=uid,
                customer=customer,
                subscription=_subscription_info(sub, refs),
                metadata=metadata,
            ),
        )
        return

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.payment_succeeded,
            occurred_at=occurred_at,
            account_id=uid,
            customer=customer,
            payment=_checkout_payment_info(session, expanded, "succeeded"),
            metadata=metadata,
        ),
    )


async def _handle_checkout_expired(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.checkout_expired,
            occurred_at=occurred_at,
            account_id=account_id or _value(data, "client_reference_id") or metadata.get("bursar_account_id"),
            customer=_customer_info(data),
            metadata=metadata,
        ),
    )


async def _handle_subscription_updated(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    status = _value(data, "status")
    if status == "canceled":
        event_type = BillingEventType.subscription_canceled
    elif _value(data, "cancel_at_period_end"):
        event_type = BillingEventType.subscription_cancellation_scheduled
    else:
        event_type = BillingEventType.subscription_updated

    customer_id = _expandable_id(_value(data, "customer"))
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            customer=(BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None),
            subscription=_subscription_info(data),
            metadata=metadata,
        ),
    )


async def _handle_subscription_lifecycle(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    billing_event_type: BillingEventType,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    customer_id = _expandable_id(_value(data, "customer"))
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=billing_event_type,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            customer=(BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None),
            subscription=_subscription_info(data),
            metadata=metadata,
        ),
    )


async def _handle_subscription_deleted(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    customer_id = _expandable_id(_value(data, "customer"))
    subscription = _subscription_info(data).model_copy(
        update={
            "status": BillingSubscriptionStatus.canceled,
            "ended_at": _timestamp(_value(data, "ended_at")) or occurred_at,
        }
    )
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.subscription_canceled,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            customer=(BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None),
            subscription=subscription,
            metadata=metadata,
        ),
    )


async def _handle_invoice_paid(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    subscription_id = _invoice_subscription_id(data)
    if not subscription_id:
        logger.debug("invoice.paid: no subscription reference", {"invoiceId": _value(data, "id")})
        return

    metadata = {**event_metadata, **_invoice_metadata(data)}
    sub = await stripe.v1.subscriptions.retrieve_async(subscription_id)
    period_start = _build_start(sub) or _build_start_from_invoice(data)
    period_end = _build_end(sub) or _build_end_from_invoice(data)
    customer_id = _expandable_id(_value(data, "customer"))
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.invoice_paid,
            occurred_at=occurred_at,
            account_id=account_id
            or metadata.get("bursar_account_id")
            or _metadata(_value(sub, "metadata")).get("bursar_account_id"),
            customer=(BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None),
            subscription=_subscription_info(sub).model_copy(
                update={"period_start": period_start, "period_end": period_end}
            ),
            invoice=BillingInvoiceInfo(
                provider_invoice_id=require_provider_string(_value(data, "id"), "Stripe invoice.id"),
                status="paid",
                amount_paid_minor=require_minor_units(_value(data, "amount_paid"), "Stripe invoice.amount_paid"),
                amount_due_minor=require_minor_units(_value(data, "amount_due"), "Stripe invoice.amount_due"),
                currency=require_currency(_value(data, "currency"), "Stripe invoice.currency"),
                period_start=period_start,
                period_end=period_end,
            ),
            metadata=metadata,
        ),
    )


async def _handle_invoice_payment_failed(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    subscription_id = _invoice_subscription_id(data)
    if not subscription_id:
        logger.debug(
            "invoice.payment_failed: no subscription reference",
            {"invoiceId": _value(data, "id")},
        )
        return
    metadata = {**event_metadata, **_invoice_metadata(data)}
    sub = await stripe.v1.subscriptions.retrieve_async(subscription_id)
    customer_id = _expandable_id(_value(data, "customer"))
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.payment_failed,
            occurred_at=occurred_at,
            account_id=account_id
            or metadata.get("bursar_account_id")
            or _metadata(_value(sub, "metadata")).get("bursar_account_id"),
            customer=(BillingCustomerInfo(provider_customer_id=customer_id) if customer_id else None),
            subscription=_subscription_info(sub),
            payment=BillingPaymentInfo(
                provider_payment_id=_invoice_payment_id(data),
                amount_minor=require_minor_units(_value(data, "subtotal"), "Stripe invoice.subtotal"),
                tax_minor=_invoice_tax_minor(data),
                currency=require_currency(_value(data, "currency"), "Stripe invoice.currency"),
                purpose="subscription",
                status="failed",
                refs=_subscription_refs(sub),
            ),
            metadata=metadata,
        ),
    )


async def _handle_payment_intent_event(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    billing_event_type: BillingEventType,
    payment_status: Literal["pending", "succeeded", "failed", "canceled"],
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    if not metadata.get("auto_recharge_attempt_id"):
        return

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=billing_event_type,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            payment=BillingPaymentInfo(
                provider_payment_id=require_provider_string(_value(data, "id"), "Stripe payment intent.id"),
                amount_minor=require_minor_units(_value(data, "amount"), "Stripe payment intent.amount"),
                tax_minor=0,
                currency=require_currency(_value(data, "currency"), "Stripe payment intent.currency"),
                purpose="credit_topup",
                status=payment_status,
                refs=ProviderRef(
                    product_id=metadata.get("product_id"),
                    price_id=metadata.get("price_id"),
                ),
            ),
            metadata=metadata,
        ),
    )


async def _handle_dispute_event(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    closed: bool,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    provider_payment_id = _expandable_id(_value(data, "payment_intent")) or _expandable_id(_value(data, "charge"))
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.dispute_closed if closed else BillingEventType.dispute_created,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            dispute=BillingDisputeInfo(
                provider_dispute_id=require_provider_string(_value(data, "id"), "Stripe dispute.id"),
                provider_payment_id=require_provider_string(
                    provider_payment_id,
                    "Stripe dispute payment identifier",
                ),
                status=_stripe_dispute_status(_value(data, "status")),
                reason=_value(data, "reason"),
            ),
            metadata=metadata,
        ),
    )


async def _handle_refund_event(
    event_id: str,
    data: Any,
    account_id: str | None,
    event_metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    billing_event_type: BillingEventType,
    forced_status: str | None = None,
) -> None:
    del stripe, logger
    metadata = {**event_metadata, **_metadata(_value(data, "metadata"))}
    match forced_status or _value(data, "status"):
        case "succeeded":
            refund_status = "succeeded"
        case "failed":
            refund_status = "failed"
        case "canceled":
            refund_status = "canceled"
        case _:
            refund_status = "pending"
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=billing_event_type,
            occurred_at=occurred_at,
            account_id=account_id or metadata.get("bursar_account_id"),
            refund=BillingRefundInfo(
                provider_refund_id=require_provider_string(_value(data, "id"), "Stripe refund.id"),
                provider_payment_id=require_provider_string(
                    _expandable_id(_value(data, "payment_intent")),
                    "Stripe refund.payment_intent",
                ),
                amount_minor=require_minor_units(_value(data, "amount"), "Stripe refund.amount", positive=True),
                currency=require_currency(_value(data, "currency"), "Stripe refund.currency"),
                reason=_value(data, "reason"),
                status=refund_status,
            ),
            metadata=metadata,
        ),
    )


_EVENT_HANDLERS: dict[str, StripeEventHandler] = {
    "checkout.session.completed": partial(_handle_checkout_event, outcome="completed"),
    "checkout.session.async_payment_succeeded": partial(_handle_checkout_event, outcome="succeeded"),
    "checkout.session.async_payment_failed": partial(_handle_checkout_event, outcome="failed"),
    "checkout.session.expired": _handle_checkout_expired,
    "customer.subscription.created": partial(
        _handle_subscription_lifecycle,
        billing_event_type=BillingEventType.subscription_created,
    ),
    "customer.subscription.paused": partial(
        _handle_subscription_lifecycle,
        billing_event_type=BillingEventType.subscription_paused,
    ),
    "customer.subscription.resumed": partial(
        _handle_subscription_lifecycle,
        billing_event_type=BillingEventType.subscription_resumed,
    ),
    "customer.subscription.trial_will_end": partial(
        _handle_subscription_lifecycle,
        billing_event_type=BillingEventType.subscription_trial_will_end,
    ),
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.paid": _handle_invoice_paid,
    "invoice.payment_failed": _handle_invoice_payment_failed,
    "payment_intent.succeeded": partial(
        _handle_payment_intent_event,
        billing_event_type=BillingEventType.payment_succeeded,
        payment_status="succeeded",
    ),
    "payment_intent.payment_failed": partial(
        _handle_payment_intent_event,
        billing_event_type=BillingEventType.payment_failed,
        payment_status="failed",
    ),
    "payment_intent.canceled": partial(
        _handle_payment_intent_event,
        billing_event_type=BillingEventType.payment_failed,
        payment_status="canceled",
    ),
    "charge.dispute.created": partial(_handle_dispute_event, closed=False),
    "charge.dispute.updated": partial(_handle_dispute_event, closed=False),
    "charge.dispute.closed": partial(_handle_dispute_event, closed=True),
    "refund.created": partial(
        _handle_refund_event,
        billing_event_type=BillingEventType.refund_created,
    ),
    "refund.updated": partial(
        _handle_refund_event,
        billing_event_type=BillingEventType.refund_updated,
    ),
    "refund.failed": partial(
        _handle_refund_event,
        billing_event_type=BillingEventType.refund_failed,
        forced_status="failed",
    ),
}


async def handle_stripe_billing_event(
    event_type: str,
    event_id: str,
    data: Any,
    account_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: StripeClient,
    logger: ProviderLogger | None = None,
    event_created: int | float | None = None,
) -> None:
    occurred_at = _timestamp(event_created)
    if occurred_at is None:
        raise ValueError("Stripe event.created is required")
    if logger is None:
        logger = _log

    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        logger.debug("Unhandled Stripe webhook event", {"eventType": event_type})
        return

    try:
        await handler(event_id, data, account_id, metadata, sink, stripe, logger, occurred_at)
    except Exception as exc:
        logger.error(
            "Stripe webhook processing failed",
            {"eventType": event_type, "err": persisted_diagnostic_summary(exc, "webhook_processing_failed")},
        )
        raise
