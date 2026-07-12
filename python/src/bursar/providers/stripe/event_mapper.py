from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from bursar.billing.manager import BillingManager
from bursar.billing.models import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingInvoiceInfo,
    BillingPaymentInfo,
    BillingSubscriptionInfo,
    ProviderRef,
)
from bursar.providers._shared import call_billing_manager, parse_status
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
    bm: BillingManager,
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

            call_billing_manager(
                bm,
                BillingEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type=BillingEventType.subscription_created,
                    occurred_at=occurred_at,
                    user_id=uid,
                    customer=customer_info,
                    subscription=BillingSubscriptionInfo(
                        provider_subscription_id=sub_id,
                        status=parse_status(sub.get("status")),
                        cancel_at_period_end=sub.get("cancel_at_period_end"),
                        period_start=period_start,
                        period_end=period_end,
                        refs=ProviderRef(lookup_key=plan_slug) if plan_slug else None,
                    ),
                ),
            )

            sub_status = sub.get("status")
            if sub_status in ("active", "trialing"):
                call_billing_manager(
                    bm,
                    BillingEvent(
                        provider="stripe",
                        event_id=event_id,
                        event_type=BillingEventType.subscription_activated,
                        occurred_at=occurred_at,
                        user_id=uid,
                        customer=customer_info,
                        subscription=BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                            status=parse_status(sub_status),
                            period_start=period_start,
                            period_end=period_end,
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

        call_billing_manager(
            bm,
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
    bm: BillingManager,
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

    call_billing_manager(
        bm,
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
            ),
        ),
    )


async def _handle_subscription_deleted(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    bm: BillingManager,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    sub = data
    call_billing_manager(
        bm,
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
            ),
        ),
    )


async def _handle_invoice_paid(
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    bm: BillingManager,
    stripe: Any,
    logger: ProviderLogger,
    occurred_at: str,
) -> None:
    invoice = data
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        logger.debug("invoice.paid: no subscription reference", {"invoiceId": invoice.get("id")})
        return

    uid = user_id
    stripe_sub: Any = None
    if uid is None:
        try:
            stripe_sub = await stripe.Subscription.retrieve_async(subscription_id)
        except Exception as exc:
            logger.error(
                "invoice.paid: failed to retrieve subscription",
                {"subscriptionId": subscription_id, "err": str(exc)},
            )
            return
        uid = (stripe_sub.get("metadata") or {}).get("userId")
        if not uid:
            logger.warning("invoice.paid: no userId", {"subscriptionId": subscription_id})
            return
    else:
        try:
            stripe_sub = await stripe.Subscription.retrieve_async(subscription_id)
        except Exception as exc:
            logger.error(
                "invoice.paid: failed to retrieve subscription",
                {"subscriptionId": subscription_id, "err": str(exc)},
            )
            return

    period_end = _build_end(stripe_sub)
    period_start = _build_start(stripe_sub)

    call_billing_manager(
        bm,
        BillingEvent(
            provider="stripe",
            event_id=event_id,
            event_type=BillingEventType.invoice_paid,
            occurred_at=occurred_at,
            user_id=uid,
            customer=BillingCustomerInfo(
                provider_customer_id=_customer_id(stripe_sub.get("customer")),
            ),
            subscription=BillingSubscriptionInfo(
                provider_subscription_id=subscription_id,
                status=parse_status(stripe_sub.get("status")),
                period_start=period_start,
                period_end=period_end,
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


_EVENT_HANDLERS: dict[str, Any] = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.paid": _handle_invoice_paid,
}


async def handle_stripe_billing_event(
    event_type: str,
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    bm: BillingManager,
    stripe: Any,
    logger: ProviderLogger | None = None,
) -> None:
    occurred_at = datetime.now(UTC).isoformat()
    if logger is None:
        logger = _log

    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        logger.debug("Unhandled Stripe webhook event", {"eventType": event_type})
        return

    try:
        await handler(event_id, data, user_id, metadata, bm, stripe, logger, occurred_at)
    except Exception as exc:
        logger.error(
            "Stripe webhook processing failed",
            {"eventType": event_type, "err": str(exc)},
        )
        raise
