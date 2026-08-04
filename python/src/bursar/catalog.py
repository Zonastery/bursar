"""Provider-secret-free product catalog projections."""

from __future__ import annotations

from typing import Any

from bursar.config.types import BursarConfig, SubscriptionOffer, TopupOffer


def _window(value: Any) -> dict[str, Any]:
    if value.type == "calendar":
        return {
            "type": value.type,
            "unit": value.unit,
            "count": value.count,
            "timezone": value.timezone,
        }
    if value.type == "plan_assignment":
        return {
            "type": value.type,
            "unit": value.interval.unit,
            "count": value.interval.count,
            "timezone": value.timezone,
        }
    return {
        "type": value.type,
        "unit": value.duration.unit,
        "count": value.duration.count,
    }


def _offer(key: str, value: SubscriptionOffer | TopupOffer) -> dict[str, Any]:
    result: dict[str, Any] = {
        "key": key,
        "type": value.type,
        "display_name": value.display_name,
        "description": value.description,
        "sort_order": value.sort_order,
        "price": value.price.model_dump(mode="json"),
    }
    if isinstance(value, SubscriptionOffer):
        result["billing_interval"] = value.billing_interval.model_dump(mode="json")
    else:
        result["credits_per_unit"] = str(value.credits_per_unit)
        result["quantity"] = value.quantity.model_dump(mode="json")
    return result


def project_public_catalog(config: BursarConfig) -> dict[str, Any]:
    """Return a JSON-safe catalog without provider product identifiers."""

    offer_items = list(config.commerce.offers.items())
    plans: list[dict[str, Any]] = []
    for key, plan in sorted(config.plans.items(), key=lambda item: (item[1].rank, item[0])):
        plan_offers = [
            _offer(offer_key, offer)
            for offer_key, offer in offer_items
            if isinstance(offer, SubscriptionOffer) and offer.plan == key
        ]
        plan_offers.sort(key=lambda offer: (offer["sort_order"], offer["key"]))
        plans.append(
            {
                "key": key,
                "display_name": plan.display_name,
                "description": plan.description,
                "rank": plan.rank,
                "features": dict(plan.features),
                "allowance": (
                    {
                        "amount": str(plan.credit_allowance.amount),
                        "priority": plan.credit_allowance.priority,
                        "window": _window(plan.credit_allowance.window),
                    }
                    if plan.credit_allowance
                    else None
                ),
                "quotas": {
                    quota_key: {
                        "operation": quota.operation,
                        "measure": quota.measure,
                        "limit": str(quota.limit),
                        "window": _window(quota.window),
                        "enforcement": quota.enforcement,
                    }
                    for quota_key, quota in plan.quotas.items()
                },
                "offers": plan_offers,
            }
        )
    topups = [_offer(key, offer) for key, offer in offer_items if isinstance(offer, TopupOffer)]
    topups.sort(key=lambda offer: (offer["sort_order"], offer["key"]))
    return {
        "version": 1,
        "default_plan": config.catalog.default_plan or (plans[0]["key"] if plans else None),
        "credit_display": (
            {
                "currency": config.credits.display.currency,
                "units_per_major": str(config.credits.display.units_per_major),
            }
            if config.credits.display
            else None
        ),
        "plans": plans,
        "topups": topups,
    }
