"""Bursar configuration validation and loading — mirrors JS SDK ``config.ts``."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from bursar.config.parse_access import _validate_admission, _validate_entitlements
from bursar.config.parse_commerce import _validate_commerce
from bursar.config.parse_credits import _validate_credits
from bursar.config.parse_plans import _validate_plan
from bursar.config.parse_pricing import _validate_pricing
from bursar.config.types import (
    AutoRechargeGuardrails,
    BooleanFeature,
    BursarConfig,
    Charge,
    ChargeUnmatched,
    CommerceConfig,
    ConfigError,
    CreditsConfig,
    CustomObjectReference,
    DodoProductReference,
    EqualMatcher,
    ExpressionCharge,
    FeatureDefinition,
    FlatCharge,
    GraduatedCharge,
    InMatcher,
    IntegerFeature,
    NotInMatcher,
    OperationPricing,
    PackageCharge,
    PerUnitCharge,
    PlanDefinition,
    PrefixMatcher,
    PriceRule,
    PricingConfig,
    RangeMatcher,
    StringFeature,
    StripePriceReference,
    SubscriptionChangePolicy,
    SubscriptionChanges,
    SubscriptionEndExpiry,
    SubscriptionOffer,
    SumCharge,
    TopupOffer,
    VolumeCharge,
    Window,
    _validate_map_keys,
)

BursarConfigData = BursarConfig
ParsedBursarConfig = BursarConfig


def _validate_feature_value(
    plan_key: str,
    feature_key: str,
    value: bool | int | str,
    definition: FeatureDefinition,
) -> None:
    path = f"plans.{plan_key}.features.{feature_key}"
    if isinstance(definition, BooleanFeature):
        if type(value) is not bool:
            raise ValueError(f"{path} must be boolean")
        return
    if isinstance(definition, IntegerFeature):
        if type(value) is not int:
            raise ValueError(f"{path} must be integer")
        if definition.minimum is not None and value < definition.minimum:
            raise ValueError(f"{path} is below the feature minimum")
        if definition.maximum is not None and value > definition.maximum:
            raise ValueError(f"{path} exceeds the feature maximum")
        return
    if isinstance(definition, StringFeature):
        if not isinstance(value, str):
            raise ValueError(f"{path} must be string")
        if definition.pattern is not None and re.search(definition.pattern, value) is None:
            raise ValueError(f"{path} does not match the feature pattern")
        return
    if value not in definition.values:
        raise ValueError(f"{path}: '{value}' must be one of {definition.values}")


def validate_bursar_config(config: BursarConfig) -> BursarConfig:  # noqa: C901
    if config.pricing is not None:
        _validate_pricing(config.pricing)
    _validate_credits(config.credits)
    _validate_entitlements(config.entitlements)
    _validate_admission(config.admission)
    _validate_commerce(config.commerce)
    _validate_map_keys(config.plans, "plans")
    if config.catalog.default_plan is not None and config.catalog.default_plan not in config.plans:
        raise ValueError(f"catalog.default_plan references unknown plan '{config.catalog.default_plan}'")
    subscription_plans = {
        offer.plan for offer in config.commerce.offers.values() if isinstance(offer, SubscriptionOffer)
    }
    for plan_key, plan in config.plans.items():
        _validate_plan(plan)
        if plan.revision_policy is None:
            plan.revision_policy = "next_renewal" if plan_key in subscription_plans else "immediate"
        if plan.rate_card is not None and (config.pricing is None or plan.rate_card not in config.pricing.rate_cards):
            raise ValueError(f"plans.{plan_key}.rate_card references unknown rate card '{plan.rate_card}'")
        if plan.allowed_operations and config.pricing is None:
            raise ValueError(f"plans.{plan_key}.allowed_operations requires pricing")
        for operation in plan.allowed_operations:
            if config.pricing is None or operation not in config.pricing.operations:
                raise ValueError(f"plans.{plan_key} references unknown operation '{operation}'")
            if plan.rate_card is None or not config.pricing.resolves_operation(plan.rate_card, operation):
                raise ValueError(f"plans.{plan_key} enables operation '{operation}' without pricing in its rate card")
        for feature_key, value in plan.features.items():
            definition = config.entitlements.features.get(feature_key)
            if definition is None:
                raise ValueError(f"plans.{plan_key}.features.{feature_key} references unknown feature '{feature_key}'")
            _validate_feature_value(plan_key, feature_key, value, definition)
        for quota_key, quota in plan.quotas.items():
            if config.pricing is None or quota.operation not in config.pricing.operations:
                raise ValueError(
                    f"plans.{plan_key}.quotas.{quota_key} references unknown operation '{quota.operation}'"
                )
            measures = config.pricing.operations[quota.operation].measures
            if quota.measure not in measures:
                raise ValueError(f"plans.{plan_key}.quotas.{quota_key} references unknown measure '{quota.measure}'")
        if plan.credit_allowance is not None:
            if config.credits.default_bucket is None:
                raise ValueError(f"plans.{plan_key}.credit_allowance requires credits.default_bucket")
            allowance_priority = plan.credit_allowance.priority
            if allowance_priority is not None and any(
                bucket.priority == allowance_priority for bucket in config.credits.buckets.values()
            ):
                raise ValueError(
                    f"plans.{plan_key}.credit_allowance.priority conflicts with "
                    f"credit bucket priority {allowance_priority}"
                )
        if plan.credit_policy is not None and plan.credit_policy not in config.credits.policies:
            raise ValueError(f"plans.{plan_key}.credit_policy references unknown policy '{plan.credit_policy}'")
        if plan.admission_policy is not None and plan.admission_policy not in config.admission.policies:
            raise ValueError(f"plans.{plan_key}.admission_policy references unknown policy '{plan.admission_policy}'")
    priorities = list(config.credits.buckets.values())
    if len({b.priority for b in priorities}) != len(priorities):
        raise ValueError("bucket priorities must be unique")
    if config.pricing is not None:
        for policy_key, policy in config.admission.policies.items():
            unknown = set(policy.operations) - set(config.pricing.operations)
            if unknown:
                raise ValueError(
                    f"admission.policies.{policy_key}.operations references unknown operations {sorted(unknown)}"
                )
    for program_key, program in config.credits.grant_programs.items():
        unknown_plans = set(program.eligibility.plans) - set(config.plans)
        if unknown_plans:
            raise ValueError(
                f"credits.grant_programs.{program_key}.eligibility references unknown plans {sorted(unknown_plans)}"
            )
        for award in program.awards:
            if award.bucket not in config.credits.buckets:
                raise ValueError(
                    f"credits.grant_programs.{program_key}.awards bucket '{award.bucket}' references unknown bucket"
                )
    for offer_key, offer in config.commerce.offers.items():
        if isinstance(offer, SubscriptionOffer):
            if offer.plan not in config.plans:
                raise ValueError(f"commerce.offers.{offer_key}.plan references unknown plan '{offer.plan}'")
            if offer.cycle_grant is not None and offer.cycle_grant.bucket not in config.credits.buckets:
                raise ValueError(f"commerce.offers.{offer_key}.cycle_grant references unknown bucket")
        else:
            if offer.bucket not in config.credits.buckets:
                raise ValueError(f"commerce.offers.{offer_key}.bucket references unknown bucket")
            if isinstance(offer.expiry, SubscriptionEndExpiry):
                raise ValueError(f"commerce.offers.{offer_key} top-up cannot use subscription_end expiry")
    auto = config.commerce.auto_recharge
    if auto is not None:
        currencies: set[str] = set()
        for topup_key in auto.eligible_topups:
            offer = config.commerce.offers.get(topup_key)
            if not isinstance(offer, TopupOffer):
                raise ValueError(f"commerce.auto_recharge references non-top-up offer '{topup_key}'")
            currencies.add(offer.price.currency)
            if auto.quantity.minimum < offer.quantity.minimum or auto.quantity.maximum > offer.quantity.maximum:
                raise ValueError(f"commerce.auto_recharge.quantity must fit commerce.offers.{topup_key}.quantity")
        if len(currencies) != 1:
            raise ValueError("commerce.auto_recharge eligible top-ups must use one currency")
        if auto.rearm_above <= auto.balance_below.maximum:
            raise ValueError("commerce.auto_recharge.rearm_above must exceed balance_below.maximum")
    return config


def load_config_from_dict(data: dict[str, Any]) -> BursarConfig:
    try:
        config = BursarConfig.model_validate(data)
        validate_bursar_config(config)
        return config
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(validation_error=exc) from exc
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def canonical_bursar_config_dict(data: dict[str, Any]) -> dict[str, Any]:
    return load_config_from_dict(data).model_dump(mode="json", exclude_none=True)


def canonical_parsed_bursar_config_dict(
    data: ParsedBursarConfig,
) -> dict[str, Any]:
    """Serialize an already parsed config without re-validating it."""
    return data.model_dump(mode="json", exclude_none=True)


__all__ = [
    "AutoRechargeGuardrails",
    "BursarConfig",
    "BursarConfigData",
    "canonical_bursar_config_dict",
    "canonical_parsed_bursar_config_dict",
    "Charge",
    "ChargeUnmatched",
    "CommerceConfig",
    "ConfigError",
    "CreditsConfig",
    "CustomObjectReference",
    "DodoProductReference",
    "EqualMatcher",
    "ExpressionCharge",
    "FeatureDefinition",
    "FlatCharge",
    "GraduatedCharge",
    "InMatcher",
    "load_config_from_dict",
    "NotInMatcher",
    "OperationPricing",
    "PackageCharge",
    "PerUnitCharge",
    "ParsedBursarConfig",
    "PlanDefinition",
    "PrefixMatcher",
    "PriceRule",
    "PricingConfig",
    "RangeMatcher",
    "StripePriceReference",
    "SubscriptionEndExpiry",
    "SubscriptionChangePolicy",
    "SubscriptionChanges",
    "SubscriptionOffer",
    "SumCharge",
    "TopupOffer",
    "validate_bursar_config",
    "VolumeCharge",
    "Window",
    "_validate_map_keys",
]
