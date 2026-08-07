from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

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
from bursar.providers._shared import (
    call_billing_event_sink,
    parse_status,
    require_currency,
    require_minor_units,
    require_provider_string,
)
from bursar.providers.types import ProviderLogger, StdlibProviderLogger

_log = StdlibProviderLogger(logging.getLogger(__name__))


def _dispute_status(
    event_type: str,
) -> Literal["needs_response", "under_review", "won", "lost", "closed"]:
    match event_type:
        case "dispute.opened":
            return "needs_response"
        case "dispute.challenged":
            return "under_review"
        case "dispute.won":
            return "won"
        case "dispute.lost" | "dispute.accepted":
            return "lost"
        case _:
            return "closed"


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
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
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
            return None
    except Exception:
        pass
    return None


def _nonempty_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _product_id(data: dict[str, Any]) -> str | None:
    direct = _nonempty_string(data.get("product_id"))
    if direct:
        return direct
    product_cart = data.get("product_cart")
    if not isinstance(product_cart, list):
        return None
    for item in product_cart:
        if isinstance(item, dict):
            product_id = _nonempty_string(item.get("product_id"))
            if product_id:
                return product_id
    return None


def _subscription_refs(data: dict[str, Any], metadata: dict[str, str]) -> ProviderRef | None:
    product_id = _product_id(data)
    if product_id:
        return ProviderRef(product_id=product_id)
    lookup_key = str(metadata.get("plan_slug") or "").strip()
    return ProviderRef(lookup_key=lookup_key) if lookup_key else None


def _subscription_id(data: dict[str, Any]) -> str:
    return require_provider_string(
        data.get("subscription_id"),
        "Dodo subscription.subscription_id",
    )


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
    customer = data.get("customer")
    nested_customer = customer if isinstance(customer, dict) else {}
    customer_id = _nonempty_string(data.get("customer_id")) or _nonempty_string(nested_customer.get("customer_id"))
    if customer_id is None:
        return None
    return BillingCustomerInfo(
        provider_customer_id=customer_id,
        email=_nonempty_string(nested_customer.get("email")),
    )


def dodo_billing_event_id(
    event_type: str,
    data: dict[str, Any],
    event_timestamp: object,
) -> str:
    resource_key = (
        "payment_id"
        if event_type.startswith("payment.")
        else "subscription_id"
        if event_type.startswith("subscription.")
        else "refund_id"
        if event_type.startswith("refund.")
        else "dispute_id"
        if event_type.startswith("dispute.")
        else "id"
    )
    source_id = data.get(resource_key) or data.get("id")
    object_id = require_provider_string(source_id, "Dodo webhook object identifier")
    occurred_at = _normalize_date(event_timestamp)
    if occurred_at is None:
        raise ValueError("Dodo webhook timestamp must be a valid instant")
    return f"dodo:{event_type}:{object_id}:{occurred_at}"


def _base_event(
    data: dict[str, Any],
    customer_info: BillingCustomerInfo | None,
    event_type: str,
    event_timestamp: object,
    metadata: dict[str, str] | None = None,
) -> dict:
    occurred_at = _normalize_date(event_timestamp)
    if occurred_at is None:
        raise ValueError("Dodo webhook timestamp must be a valid instant")
    result: dict[str, Any] = {
        "provider": "dodo",
        "event_id": dodo_billing_event_id(event_type, data, event_timestamp),
        "occurred_at": occurred_at,
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    if not user_id:
        logger.error("Dodo subscription event: no userId", {"event": event_type})
        return

    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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


async def _handle_subscription_renewed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    if not user_id:
        logger.error("Dodo subscription event: no userId", {"event": event_type})
        return

    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)

    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    sub_status = str(data.get("status", "")) or None
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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


async def _handle_subscription_plan_changed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    sub_id = _subscription_id(data)
    customer_info = _make_customer_info(data)
    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    payment_id = require_provider_string(data.get("payment_id"), "Dodo payment.payment_id")
    subscription_id = _nonempty_string(data.get("subscription_id"))
    product_id = _product_id(data)
    refs = ProviderRef(product_id=product_id) if product_id else None

    payment_info = BillingPaymentInfo(
        provider_payment_id=payment_id,
        amount_minor=require_minor_units(
            data.get("settlement_amount"),
            "Dodo payment.settlement_amount",
        ),
        tax_minor=require_minor_units(
            data.get("settlement_tax") or 0,
            "Dodo payment.settlement_tax",
        ),
        currency=require_currency(
            data.get("settlement_currency"),
            "Dodo payment.settlement_currency",
        ),
        purpose="subscription" if subscription_id else "credit_topup",
        status="succeeded",
        refs=refs,
    )

    kw: dict[str, Any] = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    payment_id = require_provider_string(data.get("payment_id"), "Dodo payment.payment_id")
    subscription_id = _nonempty_string(data.get("subscription_id"))

    kw: dict[str, Any] = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
        "event_type": BillingEventType.payment_failed,
    }
    if subscription_id:
        kw["subscription"] = BillingSubscriptionInfo(
            provider_subscription_id=subscription_id,
        )
    product_id = _product_id(data)
    kw["payment"] = BillingPaymentInfo(
        provider_payment_id=payment_id,
        amount_minor=require_minor_units(
            data.get("total_amount"),
            "Dodo payment.total_amount",
        ),
        tax_minor=require_minor_units(
            data.get("tax") or 0,
            "Dodo payment.tax",
        ),
        currency=require_currency(
            data.get("currency"),
            "Dodo payment.currency",
        ),
        purpose="subscription" if subscription_id else "credit_topup",
        status="failed",
        refs=ProviderRef(product_id=product_id) if product_id else None,
    )
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_refund(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    refund_id = require_provider_string(data.get("refund_id") or data.get("id"), "Dodo refund.refund_id")
    payment_id = require_provider_string(data.get("payment_id"), "Dodo refund.payment_id")
    reason = data.get("reason")

    refund_info = BillingRefundInfo(
        provider_refund_id=refund_id,
        provider_payment_id=payment_id,
        amount_minor=require_minor_units(
            data.get("refund_amount", data.get("amount")),
            "Dodo refund.amount",
            positive=True,
        ),
        currency=require_currency(data.get("currency"), "Dodo refund.currency"),
        reason=str(reason) if reason is not None else None,
        status="succeeded" if event_type == "refund.succeeded" else "failed",
    )

    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
        "event_type": (
            BillingEventType.refund_created if event_type == "refund.succeeded" else BillingEventType.refund_failed
        ),
        "refund": refund_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_dispute_created(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    dispute_id = require_provider_string(
        data.get("dispute_id") or data.get("id"),
        "Dodo dispute.dispute_id",
    )
    payment_id = require_provider_string(data.get("payment_id"), "Dodo dispute.payment_id")
    reason = data.get("reason")

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        status=_dispute_status(event_type),
        reason=str(reason) if reason is not None else None,
    )

    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
        "event_type": BillingEventType.dispute_created,
        "dispute": dispute_info,
    }
    call_billing_event_sink(sink, BillingEvent(**_with_user(kw, user_id)))


async def _handle_dispute_closed(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    event_timestamp: object,
    sink: BillingEventSink,
    logger: ProviderLogger,
) -> None:
    customer_info = _make_customer_info(data)
    dispute_id = require_provider_string(
        data.get("dispute_id") or data.get("id"),
        "Dodo dispute.dispute_id",
    )
    payment_id = require_provider_string(data.get("payment_id"), "Dodo dispute.payment_id")
    reason = data.get("reason")

    dispute_info = BillingDisputeInfo(
        provider_dispute_id=dispute_id,
        provider_payment_id=payment_id,
        status=_dispute_status(event_type),
        reason=str(reason) if reason is not None else None,
    )

    kw = {
        **_base_event(data, customer_info, event_type, event_timestamp, metadata),
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
    "subscription.plan_changed": _handle_subscription_plan_changed,
    "payment.succeeded": _handle_payment_succeeded,
    "payment.failed": _handle_payment_failed,
    "refund.succeeded": _handle_refund,
    "refund.failed": _handle_refund,
    "dispute.opened": _handle_dispute_created,
    "dispute.closed": _handle_dispute_closed,
    "dispute.won": _handle_dispute_closed,
    "dispute.lost": _handle_dispute_closed,
    "dispute.accepted": _handle_dispute_closed,
    "dispute.cancelled": _handle_dispute_closed,
    "dispute.challenged": _handle_dispute_created,
    "dispute.expired": _handle_dispute_closed,
}


async def handle_dodo_billing_event(
    *,
    event_type: str,
    data: dict[str, Any],
    event_timestamp: object,
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

    await handler(event_type, data, user_id, metadata, event_timestamp, sink, logger)
