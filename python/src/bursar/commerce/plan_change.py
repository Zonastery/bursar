from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from bursar.commerce.errors import (
    CommerceResourceNotFoundError,
    MissingPlanChangePolicyError,
)
from bursar.commerce.types import PlanChangeClassification
from bursar.config import SubscriptionOffer
from bursar.config.types import BursarConfig, SubscriptionChangePolicy


class ClassifiedSubscriptionChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: PlanChangeClassification
    target_interval: Literal["month", "year"]
    policy: SubscriptionChangePolicy | None = None


def classify_subscription_change(
    config: BursarConfig,
    current_plan: str,
    current_interval_value: str | None,
    target_offer: SubscriptionOffer,
) -> ClassifiedSubscriptionChange:
    current_definition = config.plans.get(current_plan)
    target_definition = config.plans.get(target_offer.plan)
    if current_definition is None or target_definition is None:
        raise CommerceResourceNotFoundError("Subscription plan is absent from the catalog")
    current_interval = (
        current_interval_value.lower()
        if current_interval_value and current_interval_value.lower() in {"month", "year"}
        else None
    )
    target_interval_value = target_offer.billing_interval.unit
    if current_interval is None or target_interval_value not in {"month", "year"}:
        raise CommerceResourceNotFoundError("Subscription cadence is unknown")
    target_interval = cast(Literal["month", "year"], target_interval_value)

    classification: PlanChangeClassification
    if target_offer.plan == current_plan and target_interval == current_interval:
        classification = "unchanged"
    elif target_definition.rank > current_definition.rank:
        classification = "upgrade"
    elif target_definition.rank < current_definition.rank:
        classification = "downgrade"
    elif target_offer.plan != current_plan:
        classification = "lateral"
    else:
        classification = "cadence_change"
    policy = (
        getattr(config.commerce.subscription_changes, classification)
        if classification != "unchanged" and config.commerce.subscription_changes is not None
        else None
    )
    if classification != "unchanged" and policy is None:
        raise MissingPlanChangePolicyError(classification)
    return ClassifiedSubscriptionChange(
        classification=classification,
        target_interval=target_interval,
        policy=policy,
    )
