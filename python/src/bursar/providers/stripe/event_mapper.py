from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from bursar.billing.types import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingInvoiceInfo,
    BillingPaymentInfo,
    BillingRefundInfo,
    BillingSubscriptionInfo,
    ProviderRef,
)
from bursar.bursar import BillingEventSink
from bursar.providers._shared import call_billing_event_sink, parse_status
from bursar.providers.types import ProviderLogger, StdlibProviderLogger

STRIPE_CHECKOUT_EXPAND = ("line_items",)

_log = StdlibProviderLogger(logging.getLogger(__name__))


def _build_end(sub: Any) -> str | None:
    raw = sub.get("current_period_end")
    if raw:
        return datetime.fromtimestamp(raw, tz=UTC).isoformat()
    return None


def _build_start(sub: Any) -> str | None:
    raw = sub.get("current_period_start")
    if raw:
        return datetime.fromtimestamp(raw, tz=UTC).isoformat()
    return None


def _timestamp(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _build_end_from_invoice(invoice: Any) -> str:
    return _timestamp(invoice.get("period_end")) or datetime.now(UTC).isoformat()


def _build_start_from_invoice(invoice: Any) -> str:
    return _timestamp(invoice.get("period_start")) or datetime.now(UTC).isoformat()


def _subscription_refs(sub: Any) -> ProviderRef | None:
    items = sub.get("items") or {}
    item_data = items.get("data") or []
    price = (item_data[0].get("price") if item_data else None) or {}
    price_id = price.get("id")
    product = price.get("product")
    product_id = (
        product
        if isinstance(product, str)
        else product.get("id")
        if hasattr(product, "get")
        else getattr(product, "id", None)
    )
    if not price_id and not product_id:
        return None
    return ProviderRef(price_id=price_id, product_id=product_id)


def _customer_id(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "get"):
        return raw.get("id")
    return getattr(raw, "id", None)


def _customer_email(raw: Any) -> str | None:
    if hasattr(raw, "get"):
        return raw.get("email")
    return getattr(raw, "email", None)


async def _handle_checkout_completed(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    session = data
    expanded = await stripe.checkout.Session.retrieve_async(
        session.get("id"),
        expand=STRIPE_CHECKOUT_EXPAND,
    )

    uid = user_id
    if not uid:
        logger.warning("Webhook: no client_reference_id", {"sessionId": session.get("id")})
        return

    cust_id = _customer_id(session.get("customer"))
    cust_email = _customer_email(session.get("customer"))
    customer_info = BillingCustomerInfo(
        provider_customer_id=cust_id,
        email=cust_email,
    )

    if session.get("mode") == "subscription" and session.get("subscription"):
        sub_id = session["subscription"]
        if not isinstance(sub_id, str):
            sub_id = sub_id.get("id")
        try:
            sub = await stripe.Subscription.retrieve_async(sub_id)
            period_end = _build_end(sub)
            period_start = _build_start(sub)
            plan_slug = (session.get("metadata") or {}).get("plan_slug")

            call_billing_event_sink(
                sink,
                BillingEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type=BillingEventType.checkout_completed,
                    occurred_at=occurred_at,
                    user_id=uid,
                    customer=customer_info,
                    subscription=BillingSubscriptionInfo(
                        provider_subscription_id=sub_id,
                        status=parse_status(sub.get("status")),
                        cancel_at_period_end=sub.get("cancel_at_period_end"),
                        period_start=period_start,
                        period_end=period_end,
                        trial_end=_timestamp(sub.get("trial_end")),
                        cancel_at=_timestamp(sub.get("cancel_at")),
                        ended_at=_timestamp(sub.get("ended_at")),
                        refs=(ProviderRef(lookup_key=plan_slug) if plan_slug else _subscription_refs(sub)),
                    ),
                ),
            )
        except Exception as exc:
            logger.error(
                "Failed to process subscription",
                {"userId": uid, "subscriptionId": sub_id, "err": str(exc)},
            )
    else:
        line_items = expanded.get("line_items", {})
        line_data = (line_items.get("data") or [{}])[0]
        price = line_data.get("price") or {}
        price_id = price.get("id")
        product_id = price.get("product")

        payment_info = BillingPaymentInfo(
            provider_payment_id=str(session.get("payment_intent") or session.get("id")),
            amount_minor=session.get("amount_total") or 0,
            currency=(session.get("currency") or "usd").upper(),
            purpose="credit_topup",
            refs=ProviderRef(
                product_id=str(product_id) if product_id else None,
                price_id=price_id,
            ),
        )

        call_billing_event_sink(
            sink,
            BillingEvent(
                provider="stripe",
                event_id=event_id,
                event_type=BillingEventType.payment_succeeded,
                occurred_at=occurred_at,
                user_id=uid,
                customer=customer_info,
                payment=payment_info,
            ),
        )


async def _handle_subscription_updated(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    sub = data
    uid = user_id or (sub.get("metadata") or {}).get("userId")
    if not uid:
        logger.debug(
            "customer.subscription.updated: no userId",
            {"subscriptionId": sub.get("id")},
        )
        return

    period_end = _build_end(sub)
    period_start = _build_start(sub)
    sub_status = sub.get("status")
    cancel_at_end = sub.get("cancel_at_period_end")

    if sub_status == "canceled":
        evt_type = BillingEventType.subscription_canceled
    elif cancel_at_end:
        evt_type = BillingEventType.subscription_cancellation_scheduled
    else:
        evt_type = BillingEventType.subscription_updated

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=evt_type,
            occurred_at=occurred_at,
            user_id=uid,
            customer=BillingCustomerInfo(
                provider_customer_id=_customer_id(sub.get("customer")),
            ),
            subscription=BillingSubscriptionInfo(
                provider_subscription_id=sub.get("id"),
                status=parse_status(sub_status),
                cancel_at_period_end=cancel_at_end,
                period_start=period_start,
                period_end=period_end,
                trial_end=_timestamp(sub.get("trial_end")),
                cancel_at=_timestamp(sub.get("cancel_at")),
                ended_at=_timestamp(sub.get("ended_at")),
                refs=_subscription_refs(sub),
            ),
        ),
    )


async def _handle_subscription_deleted(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    sub = data
    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.subscription_canceled,
            occurred_at=occurred_at,
            customer=BillingCustomerInfo(
                provider_customer_id=_customer_id(sub.get("customer")),
            ),
            subscription=BillingSubscriptionInfo(
                provider_subscription_id=sub.get("id"),
                status=parse_status("canceled"),
                period_start=_build_start(sub),
                period_end=_build_end(sub),
                trial_end=_timestamp(sub.get("trial_end")),
                cancel_at=_timestamp(sub.get("cancel_at")),
                ended_at=_timestamp(sub.get("ended_at")) or occurred_at,
                refs=_subscription_refs(sub),
            ),
        ),
    )


async def _handle_invoice_paid(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    invoice = data
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        logger.debug("invoice.paid: no subscription reference", {"invoiceId": invoice.get("id")})
        return

    invoice_metadata = invoice.get("metadata") or {}
    parent = invoice.get("parent") or {}
    subscription_details = (parent.get("subscription_details") if hasattr(parent, "get") else None) or {}
    parent_metadata = (subscription_details.get("metadata") if hasattr(subscription_details, "get") else None) or {}
    uid = invoice_metadata.get("userId") or parent_metadata.get("userId") or user_id
    stripe_sub: Any = None
    try:
        stripe_sub = await stripe.Subscription.retrieve_async(subscription_id)
        if not uid:
            uid = (stripe_sub.get("metadata") or {}).get("userId")
    except Exception as exc:
        if not uid:
            logger.error(
                "invoice.paid: failed to retrieve subscription",
                {"subscriptionId": subscription_id, "err": str(exc)},
            )
            return
    if not uid:
        logger.warning("invoice.paid: no userId", {"subscriptionId": subscription_id})
        return

    sub_status = (
        stripe_sub.get("status")
        if stripe_sub
        else "active"
        if invoice.get("collection_method") == "send_invoice"
        else "incomplete"
    )
    period_end = _build_end(stripe_sub) if stripe_sub else _build_end_from_invoice(invoice)
    period_start = _build_start(stripe_sub) if stripe_sub else _build_start_from_invoice(invoice)

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.invoice_paid,
            occurred_at=occurred_at,
            user_id=uid,
            customer=BillingCustomerInfo(
                provider_customer_id=_customer_id(invoice.get("customer")),
            ),
            subscription=BillingSubscriptionInfo(
                provider_subscription_id=subscription_id,
                status=parse_status(sub_status),
                period_start=period_start,
                period_end=period_end,
                trial_end=_timestamp(stripe_sub.get("trial_end")) if stripe_sub else None,
                cancel_at=_timestamp(stripe_sub.get("cancel_at")) if stripe_sub else None,
                ended_at=_timestamp(stripe_sub.get("ended_at")) if stripe_sub else None,
                refs=_subscription_refs(stripe_sub) if stripe_sub else None,
            ),
            invoice=BillingInvoiceInfo(
                provider_invoice_id=invoice.get("id"),
                status=invoice.get("status") or "open",
                amount_paid_minor=invoice.get("amount_paid"),
                amount_due_minor=invoice.get("amount_due"),
                currency=(invoice.get("currency") or "usd").upper(),
            ),
        ),
    )


async def _handle_payment_intent_event(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    billing_event_type: BillingEventType,
    payment_status: Literal["pending", "succeeded", "failed", "canceled"],
) -> None:
    intent = data
    md = intent.get("metadata") or {}
    if not md.get("auto_recharge_attempt_id"):
        return

    payment_info = BillingPaymentInfo(
        provider_payment_id=intent.get("id"),
        amount_minor=intent.get("amount", 0),
        currency=(intent.get("currency") or "usd").upper(),
        purpose="credit_topup",
        status=payment_status,
        refs=ProviderRef(
            product_id=md.get("product_id"),
            price_id=md.get("price_id"),
        ),
    )

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=billing_event_type,
            occurred_at=occurred_at,
            user_id=user_id or md.get("userId"),
            payment=payment_info,
            metadata=md,
        ),
    )


async def _handle_refund_event(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
    *,
    billing_event_type: BillingEventType,
    forced_status: str | None = None,
) -> None:
    refund = data
    pi = refund.get("payment_intent") or {}
    payment_intent_id = pi.get("id") if isinstance(pi, dict) else pi

    ref_md = refund.get("metadata") or {}
    match forced_status or refund.get("status"):
        case "succeeded":
            refund_status = "succeeded"
        case "failed":
            refund_status = "failed"
        case "canceled":
            refund_status = "canceled"
        case _:
            refund_status = "pending"
    refund_info = BillingRefundInfo(
        provider_refund_id=refund.get("id"),
        provider_payment_id=payment_intent_id,
        amount_minor=refund.get("amount", 0),
        currency=(refund.get("currency") or "usd").upper(),
        reason=refund.get("reason"),
        status=refund_status,
    )

    call_billing_event_sink(
        sink,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=billing_event_type,
            occurred_at=occurred_at,
            user_id=user_id or ref_md.get("userId"),
            refund=refund_info,
            metadata=ref_md,
        ),
    )


_EVENT_HANDLERS: dict[str, Any] = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.paid": _handle_invoice_paid,
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
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    stripe: Any,
    logger: ProviderLogger | None = None,
    event_created: int | float | None = None,
) -> None:
    occurred_at = _timestamp(event_created) or datetime.now(UTC).isoformat()
    if logger is None:
        logger = _log

    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        logger.debug("Unhandled Stripe webhook event", {"eventType": event_type})
        return

    try:
        await handler(event_id, data, user_id, metadata, sink, stripe, logger, occurred_at)
    except Exception as exc:
        logger.error(
            "Stripe webhook processing failed",
            {"eventType": event_type, "err": str(exc)},
        )
        raise
