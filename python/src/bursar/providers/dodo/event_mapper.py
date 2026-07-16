from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from bursar.billing.models import (
    BillingCustomerInfo,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventType,
    BillingPaymentInfo,
    BillingRefundInfo,
    BillingSubscriptionInfo,
    ProviderRef,
)
from bursar.bursar import BillingEventSink
from bursar.providers._shared import call_billing_event_sink, parse_status
from bursar.providers.types import ProviderLogger, StdlibProviderLogger

_log = StdlibProviderLogger(logging.getLogger(__name__))


def _get_nested(data: dict, *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
            if current is None:
                return None
        else:
            return getattr(current, key, None)
    return current


def _make_customer_info(data: dict[str, Any]) -> BillingCustomerInfo | None:
    cust_id = str(data.get("customer_id", ""))
    if cust_id:
        return BillingCustomerInfo(
            provider_customer_id=cust_id,
            email=_get_nested(data, "customer", "email"),
        )
    return None


def _base_event(data: dict[str, Any], customer_info: BillingCustomerInfo | None) -> dict:
    raw_id = str(data.get("id", data.get("payment_id", "")))
    return {
        "provider": "dodo",
        "event_id": raw_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "customer": customer_info,
    }


def _with_user(kw: dict, user_id: str | None) -> dict:
    if user_id:
        kw["user_id"] = user_id
    return kw


async def _handle_subscription_active(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    if not user_id:
        logger.error("Dodo subscription event: no userId", {"event": event_type})
        return

    sub_id = str(data.get("subscription_id", ""))
    period_end = data.get("next_billing_date")
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_created,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("active"),
            period_end=period_end,
            refs=ProviderRef(lookup_key=metadata.get("plan_slug")) if metadata.get("plan_slug") else None,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_renewed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    if not user_id:
        logger.error("Dodo subscription event: no userId", {"event": event_type})
        return

    sub_id = str(data.get("subscription_id", ""))
    period_end = data.get("next_billing_date")
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_activated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("active"),
            period_end=period_end,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_cancelled(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_canceled,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_expired(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_expired,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_failed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("past_due"),
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_on_hold(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("past_due"),
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_updated_event(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    period_end = data.get("next_billing_date")
    sub_status = str(data.get("status", "")) or None
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status(sub_status),
            period_end=period_end,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_cancellation_scheduled(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_cancellation_scheduled,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            cancel_at_period_end=True,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_plan_changed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = str(data.get("subscription_id", ""))
    if not sub_id:
        return
    customer_info = _make_customer_info(data)
    product_id = str(data.get("product_id", ""))
    refs = None
    if product_id:
        refs = ProviderRef(product_id=product_id)
    elif metadata.get("plan_slug"):
        refs = ProviderRef(lookup_key=metadata["plan_slug"])
    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.subscription_plan_changed,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("active"),
            refs=refs,
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_payment_succeeded(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    raw_id = str(data.get("id", data.get("payment_id", "")))
    payment_id = str(data.get("payment_id", ""))
    subscription_id = str(data.get("subscription_id", ""))
    refs = None
    product_id = data.get("product_id")
    if product_id:
        refs = ProviderRef(product_id=str(product_id))

    payment_info = BillingPaymentInfo(
        provider_payment_id=payment_id or raw_id,
        amount_minor=int(data.get("settlement_amount", data.get("amount", 0))),
        tax_minor=int(data["settlement_tax"]) if data.get("settlement_tax") is not None else None,
        currency=str(data.get("settlement_currency", data.get("currency", "USD"))).upper(),
        purpose="subscription" if subscription_id else "credit_topup",
        refs=refs,
    )

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.payment_succeeded,
        "payment": payment_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_payment_failed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    raw_id = str(data.get("id", data.get("payment_id", "")))
    payment_id = str(data.get("payment_id", ""))
    subscription_id = str(data.get("subscription_id", ""))

    payment_info = BillingPaymentInfo(
        provider_payment_id=payment_id or raw_id,
        amount_minor=int(data.get("settlement_amount", data.get("amount", 0))),
        currency=str(data.get("settlement_currency", data.get("currency", "USD"))).upper(),
        purpose="subscription" if subscription_id else "unknown",
    )

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.payment_failed,
        "payment": payment_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_refund_succeeded(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    refund_id = str(data.get("id", ""))
    payment_id = str(data.get("payment_id", ""))

    refund_info = BillingRefundInfo(
        provider_refund_id=refund_id,
        provider_payment_id=payment_id,
        amount_minor=int(data.get("amount", 0)),
        currency=str(data.get("currency", "USD")).upper(),
        reason=str(data.get("reason", "")),
    )

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.refund_created,
        "refund": refund_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_dispute_created(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    dispute_id = str(data.get("id", ""))
    payment_id = str(data.get("payment_id", ""))

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        status="needs_response",
        reason=str(data.get("reason", "")),
    )

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.dispute_created,
        "dispute": dispute_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_dispute_closed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    dispute_id = str(data.get("id", ""))
    payment_id = str(data.get("payment_id", ""))

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        status=str(data.get("status", "closed")),
        reason=str(data.get("reason", "")),
    )

    kw = {
        **_base_event(data, customer_info),
        "event_type": BillingEventType.dispute_closed,
        "dispute": dispute_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


_EVENT_HANDLERS: dict[str, Any] = {
    "subscription.active": _handle_subscription_active,
    "subscription.renewed": _handle_subscription_renewed,
    "subscription.cancelled": _handle_subscription_cancelled,
    "subscription.expired": _handle_subscription_expired,
    "subscription.failed": _handle_subscription_failed,
    "subscription.on_hold": _handle_subscription_on_hold,
    "subscription.updated": _handle_subscription_updated_event,
    "subscription.cancellation_scheduled": _handle_subscription_cancellation_scheduled,
    "subscription.plan_changed": _handle_subscription_plan_changed,
    "payment.succeeded": _handle_payment_succeeded,
    "payment.failed": _handle_payment_failed,
    "refund.succeeded": _handle_refund_succeeded,
    "dispute.created": _handle_dispute_created,
    "dispute.closed": _handle_dispute_closed,
}


async def handle_dodo_billing_event(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger | None = None,
) -> None:
    if logger is None:
        logger = _log

    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        logger.debug("Unhandled Dodo webhook event type", {"type": event_type})
        return

    await handler(event_type, data, user_id, metadata, sink, logger)
