from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bursar.billing.manager import BillingManager
from bursar.billing.models import (
    BillingCustomerInfo,
    BillingEvent,
    BillingInvoiceInfo,
    BillingPaymentInfo,
    BillingSubscriptionInfo,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.providers.types import ProviderLogger

STRIPE_CHECKOUT_EXPAND = ["line_items"]


def _parse_status(raw: Any) -> BillingSubscriptionStatus | None:
    if raw is None:
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None


def _build_end(sub: Any) -> str | None:
    raw = sub.get("current_period_end")
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


def _call_billing_manager(bm: BillingManager, event: BillingEvent) -> None:
    result = bm.handle_event(event)
    if not result.handled and result.error not in ("unhandled_event_type", "user_not_found"):
        raise RuntimeError(f"BillingManager failed to handle event: {result.error}")


async def handle_stripe_billing_event(  # noqa: C901
    event_type: str,
    event_id: str,
    data: Any,
    user_id: str | None,
    metadata: dict[str, str],
    bm: BillingManager,
    stripe: Any,
    logger: ProviderLogger | None = None,
) -> None:
    try:
        if event_type == "checkout.session.completed":
            session = data
            uid = user_id or session.get("client_reference_id")
            if not uid:
                if logger:
                    logger.warn("Webhook: no client_reference_id", {"sessionId": session.get("id")})
                return

            cust_id = _customer_id(session.get("customer"))
            cust_email = _customer_email(session.get("customer"))

            customer_info = BillingCustomerInfo(
                provider_customer_id=cust_id,
                email=cust_email,
            )

            occurred_at = datetime.now(UTC).isoformat()

            _call_billing_manager(
                bm,
                BillingEvent(
                    provider="stripe",
                    event_id=f"{event_id}_checkout",
                    event_type="checkout.completed",
                    occurred_at=occurred_at,
                    user_id=uid,
                    customer=customer_info,
                ),
            )

            if session.get("mode") == "subscription" and session.get("subscription"):
                sub_id = session["subscription"]
                if not isinstance(sub_id, str):
                    sub_id = sub_id.get("id")
                try:
                    sub = await stripe.Subscription.retrieve_async(sub_id)
                    period_end = _build_end(sub)
                    plan_slug = (session.get("metadata") or {}).get("plan_slug")

                    _call_billing_manager(
                        bm,
                        BillingEvent(
                            provider="stripe",
                            event_id=f"{event_id}_sub",
                            event_type="subscription.created",
                            occurred_at=occurred_at,
                            user_id=uid,
                            customer=customer_info,
                            subscription=BillingSubscriptionInfo(
                                provider_subscription_id=sub_id,
                                status=_parse_status(sub.get("status")),
                                cancel_at_period_end=sub.get("cancel_at_period_end"),
                                period_end=period_end,
                                refs=ProviderRef(
                                    lookup_key=plan_slug,
                                )
                                if plan_slug
                                else None,
                            ),
                        ),
                    )

                    sub_status = sub.get("status")
                    if sub_status in ("active", "trialing"):
                        _call_billing_manager(
                            bm,
                            BillingEvent(
                                provider="stripe",
                                event_id=f"{event_id}_sub_activated",
                                event_type="subscription.activated",
                                occurred_at=occurred_at,
                                user_id=uid,
                                customer=customer_info,
                                subscription=BillingSubscriptionInfo(
                                    provider_subscription_id=sub_id,
                                    status=_parse_status(sub_status),
                                    period_end=period_end,
                                ),
                            ),
                        )
                except Exception as exc:
                    if logger:
                        logger.error(
                            "Failed to process subscription",
                            {"userId": uid, "subscriptionId": sub_id, "err": str(exc)},
                        )
            else:
                expanded = await stripe.checkout.Session.retrieve_async(
                    session.get("id"),
                    expand=STRIPE_CHECKOUT_EXPAND,
                )
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

                _call_billing_manager(
                    bm,
                    BillingEvent(
                        provider="stripe",
                        event_id=f"{event_id}_payment",
                        event_type="payment.succeeded",
                        occurred_at=occurred_at,
                        user_id=uid,
                        customer=customer_info,
                        payment=payment_info,
                    ),
                )

        elif event_type == "customer.subscription.updated":
            sub = data
            uid = user_id or (sub.get("metadata") or {}).get("userId")
            if not uid:
                if logger:
                    logger.debug(
                        "customer.subscription.updated: no userId",
                        {"subscriptionId": sub.get("id")},
                    )
                return

            period_end = _build_end(sub)
            sub_status = sub.get("status")
            cancel_at_end = sub.get("cancel_at_period_end")

            if sub_status == "canceled":
                evt_type = "subscription.canceled"
            elif cancel_at_end:
                evt_type = "subscription.cancellation_scheduled"
            else:
                evt_type = "subscription.updated"

            _call_billing_manager(
                bm,
                BillingEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type=evt_type,
                    occurred_at=datetime.now(UTC).isoformat(),
                    user_id=uid,
                    customer=BillingCustomerInfo(
                        provider_customer_id=_customer_id(sub.get("customer")),
                    ),
                    subscription=BillingSubscriptionInfo(
                        provider_subscription_id=sub.get("id"),
                        status=_parse_status(sub_status),
                        cancel_at_period_end=cancel_at_end,
                        period_end=period_end,
                    ),
                ),
            )

        elif event_type == "customer.subscription.deleted":
            sub = data
            _call_billing_manager(
                bm,
                BillingEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type="subscription.canceled",
                    occurred_at=datetime.now(UTC).isoformat(),
                    customer=BillingCustomerInfo(
                        provider_customer_id=_customer_id(sub.get("customer")),
                    ),
                    subscription=BillingSubscriptionInfo(
                        provider_subscription_id=sub.get("id"),
                    ),
                ),
            )

        elif event_type == "invoice.paid":
            invoice = data
            subscription_id = invoice.get("subscription")
            if not subscription_id:
                if logger:
                    logger.debug("invoice.paid: no subscription reference", {"invoiceId": invoice.get("id")})
                return

            try:
                stripe_sub = await stripe.Subscription.retrieve_async(subscription_id)
            except Exception as exc:
                if logger:
                    logger.error(
                        "invoice.paid: failed to retrieve subscription",
                        {"subscriptionId": subscription_id, "err": str(exc)},
                    )
                return

            uid = user_id or (stripe_sub.get("metadata") or {}).get("userId")
            if not uid:
                if logger:
                    logger.warn("invoice.paid: no userId", {"subscriptionId": subscription_id})
                return

            period_end = _build_end(stripe_sub)

            _call_billing_manager(
                bm,
                BillingEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type="invoice.paid",
                    occurred_at=datetime.now(UTC).isoformat(),
                    user_id=uid,
                    customer=BillingCustomerInfo(
                        provider_customer_id=_customer_id(stripe_sub.get("customer")),
                    ),
                    subscription=BillingSubscriptionInfo(
                        provider_subscription_id=subscription_id,
                        status=_parse_status(stripe_sub.get("status")),
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

        else:
            if logger:
                logger.debug("Unhandled Stripe webhook event", {"eventType": event_type})

    except Exception as exc:
        if logger:
            logger.error(
                "Stripe webhook processing failed",
                {"eventType": event_type, "err": str(exc)},
            )
        raise
