"""Provider-secret-free product catalog projections."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from bursar.config.types import BursarConfig, FeatureValue, SubscriptionOffer, TopupOffer


class PublicCatalogWindow(TypedDict):
    type: Literal["calendar", "rolling", "fixed", "plan_assignment"]
    unit: str
    count: int
    timezone: NotRequired[str]


class PublicCatalogPrice(TypedDict):
    amount_minor: int
    currency: str
    tax_behavior: Literal["inclusive", "exclusive", "unspecified"]


class PublicCatalogInterval(TypedDict):
    unit: str
    count: int


class PublicCatalogQuantity(TypedDict):
    minimum: int
    maximum: int
    default: int


class PublicCatalogOffer(TypedDict):
    key: str
    type: Literal["subscription", "topup"]
    display_name: str
    description: NotRequired[str]
    sort_order: int
    price: PublicCatalogPrice
    billing_interval: NotRequired[PublicCatalogInterval]
    credits_per_unit: NotRequired[str]
    quantity: NotRequired[PublicCatalogQuantity]


class PublicCatalogAllowance(TypedDict):
    amount: str
    priority: int
    window: PublicCatalogWindow


class PublicCatalogQuota(TypedDict):
    operation: str
    measure: str
    limit: str
    window: PublicCatalogWindow
    enforcement: Literal["block", "allow"]


class PublicCatalogPlan(TypedDict):
    key: str
    display_name: str
    description: NotRequired[str]
    rank: int
    features: dict[str, FeatureValue]
    allowance: NotRequired[PublicCatalogAllowance]
    quotas: dict[str, PublicCatalogQuota]
    offers: list[PublicCatalogOffer]


class PublicCatalogDisplay(TypedDict):
    currency: str
    units_per_major: str


class PublicCatalog(TypedDict):
    version: Literal[1]
    default_plan: str | None
    credit_display: PublicCatalogDisplay | None
    plans: list[PublicCatalogPlan]
    topups: list[PublicCatalogOffer]


def _window(value: Any) -> PublicCatalogWindow:
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


def _offer(key: str, value: SubscriptionOffer | TopupOffer) -> PublicCatalogOffer:
    result: PublicCatalogOffer = {
        "key": key,
        "type": value.type,
        "display_name": value.display_name,
        "sort_order": value.sort_order,
        "price": {
            "amount_minor": value.price.amount_minor,
            "currency": value.price.currency,
            "tax_behavior": value.price.tax_behavior,
        },
    }
    if value.description:
        result["description"] = value.description
    if isinstance(value, SubscriptionOffer):
        result["billing_interval"] = {
            "unit": value.billing_interval.unit,
            "count": value.billing_interval.count,
        }
    else:
        result["credits_per_unit"] = str(value.credits_per_unit)
        result["quantity"] = {
            "minimum": value.quantity.minimum,
            "maximum": value.quantity.maximum,
            "default": value.quantity.default,
        }
    return result


def project_public_catalog(config: BursarConfig) -> PublicCatalog:
    """Return a JSON-safe catalog without provider product identifiers."""

    offer_items = list(config.commerce.offers.items())
    plans: list[PublicCatalogPlan] = []
    for key, plan in sorted(config.plans.items(), key=lambda item: (item[1].rank, item[0])):
        plan_offers = [
            _offer(offer_key, offer)
            for offer_key, offer in offer_items
            if isinstance(offer, SubscriptionOffer) and offer.plan == key
        ]
        plan_offers.sort(key=lambda offer: (offer["sort_order"], offer["key"]))
        public_plan: PublicCatalogPlan = {
            "key": key,
            "display_name": plan.display_name,
            "rank": plan.rank,
            "features": dict(plan.features),
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
        if plan.description:
            public_plan["description"] = plan.description
        if plan.credit_allowance:
            public_plan["allowance"] = {
                "amount": str(plan.credit_allowance.amount),
                "priority": plan.credit_allowance.priority,
                "window": _window(plan.credit_allowance.window),
            }
        plans.append(public_plan)
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
