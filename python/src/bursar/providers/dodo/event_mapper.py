from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from bursar.billing.types import (
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


def _normalize_interval(value: Any) -> str | None:
    interval = str(value or "").lower()
    return interval if interval in ("day", "week", "month", "year") else None


def _normalize_date(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw)
    try:
        return datetime.fromisoformat(s).astimezone(UTC).isoformat()
    except (ValueError, TypeError):
        pass
    try:
        import email.utils
        import re

        cleaned = re.sub(r"\s*\([^)]*\)", "", s)
        fixed = re.sub(r"\s*GMT([+-]\d{4})\s*", r" \1 ", cleaned)
        fixed = re.sub(r"\s*GMT\s*", " +0000 ", fixed).strip()
        parsed = email.utils.parsedate_to_datetime(fixed)
        if parsed and parsed.tzinfo:
            return parsed.astimezone(UTC).isoformat()
        if parsed:
            return parsed.isoformat()
    except Exception:
        pass
    return None


def _subscription_refs(data: dict[str, Any], metadata: dict[str, str]) -> ProviderRef | None:
    product_id = str(data.get("product_id") or "").strip()
    if product_id:
        return ProviderRef(product_id=product_id)
    lookup_key = str(metadata.get("plan_slug") or "").strip()
    return ProviderRef(lookup_key=lookup_key) if lookup_key else None


def _subscription_fields(data: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
    interval = (
        _normalize_interval(data.get("payment_frequency_interval"))
        or _normalize_interval(data.get("subscription_period_interval"))
        or _normalize_interval(metadata.get("billing_interval"))
    )
    raw_interval_count = data.get("payment_frequency_count") or data.get("subscription_period_count")
    if raw_interval_count is None and interval:
        raw_interval_count = 1
    result: dict[str, Any] = {}
    if interval:
        result["interval"] = interval
    if raw_interval_count is not None:
        try:
            ic = int(raw_interval_count)
            if ic > 0:
                result["interval_count"] = ic
        except (ValueError, TypeError):
            pass
    ps = _normalize_date(data.get("previous_billing_date"))
    if ps:
        result["period_start"] = ps
    return result


def _make_customer_info(data: dict[str, Any]) -> BillingCustomerInfo | None:
    cust_id = str(data.get("customer_id", ""))
    if cust_id:
        return BillingCustomerInfo(
            provider_customer_id=cust_id,
            email=_get_nested(data, "customer", "email"),
        )
    return None


def _base_event(
    data: dict[str, Any],
    customer_info: BillingCustomerInfo | None,
    event_type: str | None = None,
    metadata: dict[str, str] | None = None,
) -> dict:
    source_id = data.get("id") or data.get("refund_id") or data.get("payment_id")
    if source_id:
        raw_id = str(source_id)
    else:
        sub_or_cust = data.get("subscription_id") or data.get("customer_id") or ""
        raw_id = f"dodo:{event_type or ''}:{sub_or_cust}"
    result: dict[str, Any] = {
        "provider": "dodo",
        "event_id": raw_id,
        "occurred_at": (
            _normalize_date(data.get("updated_at") or data.get("created_at") or data.get("timestamp"))
            or datetime.now(UTC).isoformat()
        ),
        "customer": customer_info,
    }
    if metadata:
        result["metadata"] = metadata
    return result


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
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_created,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status(data.get("status", "active")),
            period_end=_normalize_date(data.get("next_billing_date")),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_checkout_expired(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    del logger
    kw = {
        **_base_event(data, _make_customer_info(data), event_type, metadata),
        "event_type": BillingEventType.checkout_expired,
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
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_renewed,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("active"),
            period_end=_normalize_date(data.get("next_billing_date")),
            **_subscription_fields(data, metadata),
            refs=_subscription_refs(data, metadata),
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_canceled,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("canceled"),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_expired,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("expired"),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("past_due"),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("past_due"),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
    sub_status = str(data.get("status", "")) or None
    kw = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_updated,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status(sub_status),
            period_end=_normalize_date(data.get("next_billing_date")),
            **_subscription_fields(data, metadata),
            refs=_subscription_refs(data, metadata),
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_cancellation_scheduled,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            cancel_at_period_end=True,
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
        ),
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_subscription_cancellation_unscheduled(
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
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_cancellation_unscheduled,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            cancel_at_period_end=False,
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
    kw = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.subscription_plan_changed,
        "subscription": BillingSubscriptionInfo(
            provider_subscription_id=sub_id,
            status=parse_status("active"),
            refs=_subscription_refs(data, metadata),
            **_subscription_fields(data, metadata),
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
        status="succeeded",
        refs=refs,
    )

    kw: dict[str, Any] = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.payment_succeeded,
        "payment": payment_info,
    }
    if subscription_id:
        kw["subscription"] = BillingSubscriptionInfo(
            provider_subscription_id=subscription_id,
            status=parse_status(data.get("subscription_status", "active")),
            period_start=_normalize_date(data.get("previous_billing_date")),
            period_end=_normalize_date(data.get("next_billing_date")),
        )
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

    kw: dict[str, Any] = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.payment_failed,
    }
    if subscription_id:
        kw["subscription"] = BillingSubscriptionInfo(
            provider_subscription_id=subscription_id,
        )
    if payment_id or data.get("settlement_amount"):
        product_id = data.get("product_id")
        kw["payment"] = BillingPaymentInfo(
            provider_payment_id=payment_id or raw_id,
            amount_minor=int(data.get("settlement_amount", data.get("amount", 0))),
            tax_minor=(int(data["settlement_tax"]) if data.get("settlement_tax") is not None else None),
            currency=str(data.get("settlement_currency", data.get("currency", "USD"))).upper(),
            purpose="subscription",
            status="failed",
            refs=ProviderRef(product_id=str(product_id)) if product_id else None,
        )
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
    refund_id = str(data.get("refund_id") or data.get("id") or "")
    payment_id = str(data.get("payment_id") or "") or None
    reason = data.get("reason")

    refund_info = BillingRefundInfo(
        provider_refund_id=refund_id,
        provider_payment_id=payment_id,
        amount_minor=int(data.get("refund_amount", data.get("amount", 0))),
        currency=str(data.get("currency", "USD")).upper(),
        reason=str(reason) if reason is not None else None,
        status="succeeded",
    )

    kw = {
        **_base_event(data, customer_info, event_type, metadata),
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
    dispute_id = str(data.get("dispute_id") or data.get("id") or "")
    payment_id = str(data.get("payment_id") or "") or None
    reason = data.get("reason")

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        reason=str(reason) if reason is not None else None,
    )

    kw = {
        **_base_event(data, customer_info, event_type, metadata),
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
    dispute_id = str(data.get("dispute_id") or data.get("id") or "")
    payment_id = str(data.get("payment_id") or "") or None
    reason = data.get("reason")

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        reason=str(reason) if reason is not None else None,
    )

    kw = {
        **_base_event(data, customer_info, event_type, metadata),
        "event_type": BillingEventType.dispute_closed,
        "dispute": dispute_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


_EVENT_HANDLERS: dict[str, Any] = {
    "checkout.expired": _handle_checkout_expired,
    "subscription.active": _handle_subscription_active,
    "subscription.renewed": _handle_subscription_renewed,
    "subscription.cancelled": _handle_subscription_cancelled,
    "subscription.expired": _handle_subscription_expired,
    "subscription.failed": _handle_subscription_failed,
    "subscription.on_hold": _handle_subscription_on_hold,
    "subscription.updated": _handle_subscription_updated_event,
    "subscription.cancellation_scheduled": _handle_subscription_cancellation_scheduled,
    "subscription.cancellation_unscheduled": _handle_subscription_cancellation_unscheduled,
    "subscription.plan_changed": _handle_subscription_plan_changed,
    "payment.succeeded": _handle_payment_succeeded,
    "payment.failed": _handle_payment_failed,
    "refund.succeeded": _handle_refund_succeeded,
    "dispute.created": _handle_dispute_created,
    "dispute.closed": _handle_dispute_closed,
    "dispute.won": _handle_dispute_closed,
    "dispute.lost": _handle_dispute_closed,
    "dispute.accepted": _handle_dispute_closed,
    "dispute.cancelled": _handle_dispute_closed,
    "dispute.challenged": _handle_dispute_closed,
    "dispute.expired": _handle_dispute_closed,
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
