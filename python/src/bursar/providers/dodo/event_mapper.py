from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bursar.billing.manager import BillingManager
from bursar.billing.models import (
    BillingCustomerInfo,
    BillingEvent,
    BillingPaymentInfo,
    BillingProviderRefs,
    BillingSubscriptionInfo,
    BillingSubscriptionStatus,
)
from bursar.providers.types import ProviderLogger


def _parse_status(raw: str | None) -> BillingSubscriptionStatus | None:
    if raw is None:
        return None
    try:
        return BillingSubscriptionStatus(raw)
    except ValueError:
        return None


async def handle_dodo_billing_event(  # noqa: C901
    event_type: str,
    data: dict[str, Any],
    user_id: str | None,
    metadata: dict[str, str],
    bm: BillingManager,
    logger: ProviderLogger | None = None,
) -> None:
    raw_id = str(data.get("id", data.get("payment_id", "")))

    cust_id = str(data.get("customer_id", ""))
    customer_info = None
    if cust_id:
        customer_info = BillingCustomerInfo(
            provider_customer_id=cust_id,
            email=_get_nested(data, "customer", "email"),
        )

    def _base(event_suffix: str) -> dict:
        return {
            "provider": "dodo",
            "event_id": f"{raw_id}_{event_suffix}" if raw_id and event_suffix else raw_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "customer": customer_info,
        }

    def _with_user(kw: dict) -> dict:
        if user_id:
            kw["user_id"] = user_id
        return kw

    if event_type in ("subscription.active", "subscription.renewed"):
        if not user_id:
            if logger:
                logger.error("Dodo subscription event: no userId", {"event": event_type})
            return

        sub_id = str(data.get("subscription_id", ""))
        period_end = data.get("next_billing_date")

        if event_type == "subscription.active":
            bm.handle_event(
                BillingEvent(
                    **_with_user(
                        {
                            **_base("created"),
                            "event_type": "subscription.created",
                            "subscription": BillingSubscriptionInfo(
                                provider_subscription_id=sub_id,
                                status=_parse_status("active"),
                                period_end=period_end,
                                refs=BillingProviderRefs(
                                    lookup_key=metadata.get("plan_slug"),
                                )
                                if metadata.get("plan_slug")
                                else None,
                            ),
                        }
                    )
                )
            )

        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base("activated"),
                        "event_type": "subscription.activated",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                            status=_parse_status("active"),
                            period_end=period_end,
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.cancelled":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.canceled",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.expired":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.expired",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.failed":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.updated",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                            status=_parse_status("past_due"),
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.on_hold":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.paused",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.updated":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        period_end = data.get("next_billing_date")
        sub_status = str(data.get("status", "")) or None
        kw = {
            **_base(""),
            "event_type": "subscription.updated",
            "subscription": BillingSubscriptionInfo(
                provider_subscription_id=sub_id,
                status=_parse_status(sub_status),
                period_end=period_end,
            ),
        }
        bm.handle_event(BillingEvent(**_with_user(kw)))

    elif event_type == "subscription.cancellation_scheduled":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.cancellation_scheduled",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                            cancel_at_period_end=True,
                        ),
                    }
                )
            )
        )

    elif event_type == "subscription.plan_changed":
        sub_id = str(data.get("subscription_id", ""))
        if not sub_id:
            return
        product_id = str(data.get("product_id", ""))
        refs = None
        if product_id:
            refs = BillingProviderRefs(product_id=product_id)
        elif metadata.get("plan_slug"):
            refs = BillingProviderRefs(lookup_key=metadata["plan_slug"])
        bm.handle_event(
            BillingEvent(
                **_with_user(
                    {
                        **_base(""),
                        "event_type": "subscription.plan_changed",
                        "subscription": BillingSubscriptionInfo(
                            provider_subscription_id=sub_id,
                            status=_parse_status("active"),
                            refs=refs,
                        ),
                    }
                )
            )
        )

    elif event_type == "payment.succeeded":
        payment_id = str(data.get("payment_id", ""))
        subscription_id = str(data.get("subscription_id", ""))
        refs = None
        product_id = data.get("product_id")
        if product_id:
            refs = BillingProviderRefs(product_id=str(product_id))

        payment_info = BillingPaymentInfo(
            provider_payment_id=payment_id or raw_id,
            amount_minor=int(data.get("settlement_amount", data.get("amount", 0))),
            tax_minor=int(data["settlement_tax"]) if data.get("settlement_tax") is not None else None,
            currency=str(data.get("settlement_currency", data.get("currency", "USD"))).upper(),
            purpose="subscription" if subscription_id else "credit_topup",
            refs=refs,
        )

        kw = {
            **_base(""),
            "event_type": "payment.succeeded",
            "payment": payment_info,
        }
        if user_id:
            kw["user_id"] = user_id
        bm.handle_event(BillingEvent(**kw))

    else:
        if logger:
            logger.debug("Unhandled Dodo webhook event type", {"type": event_type})


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
