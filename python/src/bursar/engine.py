"""Typed operation pricing engine for the Bursar version-1 catalog."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from bursar.breakdown import CostBreakdown
from bursar.config import (
    BursarConfig,
    Charge,
    ChargeUnmatched,
    ConfigError,
    EqualMatcher,
    ExpressionCharge,
    FlatCharge,
    GraduatedCharge,
    InMatcher,
    NotInMatcher,
    OperationPricing,
    PackageCharge,
    PerUnitCharge,
    PrefixMatcher,
    PriceRule,
    RangeMatcher,
    SumCharge,
    VolumeCharge,
    load_config_from_dict,
)
from bursar.expr import evaluate_expression
from bursar.metrics import METRIC_VARIABLES, UsageMetrics

__all__ = ["METRIC_VARIABLES", "PricingEngine"]

_QUANTUM = Decimal("0.000001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


class PricingEngine:
    """Calculate exact credit costs from typed catalog charge rules."""

    def __init__(self, config: BursarConfig):
        self._config = config

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PricingEngine:
        return cls(load_config_from_dict(data))

    @property
    def pricing_schema(self) -> dict[str, Any]:
        return self._config.model_dump(mode="json", exclude_none=True)

    @property
    def min_balance(self) -> Decimal:
        """Compatibility accessor; account-specific credit policy owns this value."""

        return Decimal(0)

    def calculate(self, metrics: UsageMetrics, *, rate_card: str | None = None) -> CostBreakdown:
        pricing = self._config.pricing
        if pricing is None:
            raise ConfigError("usage pricing not configured")
        operation_name = metrics.operation
        definition = pricing.operations.get(operation_name)
        if definition is None:
            raise ConfigError(f"unknown usage operation '{operation_name}'")

        unknown_measures = set(metrics.measures) - set(definition.measures)
        unknown_dimensions = set(metrics.dimensions) - set(definition.dimensions)
        if unknown_measures:
            raise ConfigError(f"operation '{operation_name}' received undeclared measures {sorted(unknown_measures)}")
        if unknown_dimensions:
            raise ConfigError(
                f"operation '{operation_name}' received undeclared dimensions {sorted(unknown_dimensions)}"
            )
        for name, dimension in definition.dimensions.items():
            value = metrics.dimensions.get(name)
            if value is None:
                if dimension.required:
                    raise ConfigError(f"operation '{operation_name}' requires dimension '{name}'")
                continue
            if dimension.type == "string" and not isinstance(value, str):
                raise ConfigError(f"dimension '{name}' must be string")
            if dimension.type == "number" and not isinstance(value, Decimal):
                raise ConfigError(f"dimension '{name}' must be numeric")
            if dimension.type == "boolean" and type(value) is not bool:
                raise ConfigError(f"dimension '{name}' must be boolean")

        measures = {name: metrics.measures.get(name, Decimal(0)) for name in definition.measures}
        card_key = self._resolve_rate_card(rate_card)
        operation_price = self._operation_pricing(card_key, operation_name)
        rule = next(
            (candidate for candidate in operation_price.rules if self._matches(candidate, metrics.dimensions)),
            None,
        )
        if rule is not None:
            charge = rule.charge
        elif isinstance(operation_price.unmatched, ChargeUnmatched):
            charge = operation_price.unmatched.charge
        else:
            raise ConfigError(f"no price rule matched operation '{operation_name}' in rate card '{card_key}'")

        value = self._evaluate_charge(charge, measures)
        if not value.is_finite() or value < 0:
            raise ConfigError(f"price charge for '{operation_name}' produced a negative or non-finite credit cost")
        total = _q(value)
        return CostBreakdown(
            model_credits=Decimal(0),
            tool_credits=Decimal(0),
            search_credits=Decimal(0),
            cache_savings=Decimal(0),
            fixed_credits=Decimal(0),
            operation_credits=total,
            total=total,
            breakdown={
                "operation": operation_name,
                "rate_card": card_key,
                "charge_type": charge.type,
                "measures": {key: str(value) for key, value in measures.items()},
                "dimensions": metrics.dimensions,
            },
        )

    def calculate_batch(self, metrics: list[UsageMetrics], *, rate_card: str | None = None) -> list[CostBreakdown]:
        return [self.calculate(item, rate_card=rate_card) for item in metrics]

    def get_rate_card_for_plan(self, plan_id: str | None) -> str | None:
        if not plan_id:
            return None
        plan = self._config.plans.get(plan_id)
        return None if plan is None else plan.rate_card

    def _resolve_rate_card(self, requested: str | None) -> str:
        assert self._config.pricing is not None
        cards = self._config.pricing.rate_cards
        if requested is not None:
            if requested not in cards:
                raise ConfigError(f"unknown rate card '{requested}'")
            return requested
        if len(cards) == 1:
            return next(iter(cards))
        raise ConfigError("rate_card is required when more than one rate card is configured")

    def _operation_pricing(self, card_key: str, operation: str) -> OperationPricing:
        assert self._config.pricing is not None
        card = self._config.pricing.rate_cards[card_key]
        if operation in card.operations:
            return card.operations[operation]
        if card.extends is None:
            raise ConfigError(f"rate card '{card_key}' has no price for operation '{operation}'")
        return self._operation_pricing(card.extends, operation)

    @staticmethod
    def _matches(rule: PriceRule, dimensions: dict[str, str | Decimal | bool]) -> bool:
        for name, matcher in rule.when.items():
            if name not in dimensions:
                return False
            value = dimensions[name]
            if isinstance(matcher, EqualMatcher) and value != matcher.value:
                return False
            if isinstance(matcher, InMatcher) and value not in matcher.values:
                return False
            if isinstance(matcher, NotInMatcher) and value in matcher.values:
                return False
            if isinstance(matcher, PrefixMatcher) and (
                not isinstance(value, str) or not value.startswith(matcher.value)
            ):
                return False
            if isinstance(matcher, RangeMatcher):
                if not isinstance(value, Decimal):
                    return False
                if matcher.gt is not None and value <= matcher.gt:
                    return False
                if matcher.gte is not None and value < matcher.gte:
                    return False
                if matcher.lt is not None and value >= matcher.lt:
                    return False
                if matcher.lte is not None and value > matcher.lte:
                    return False
        return True

    def _evaluate_charge(self, charge: Charge, measures: dict[str, Decimal]) -> Decimal:
        if isinstance(charge, FlatCharge):
            return charge.amount
        if isinstance(charge, PerUnitCharge):
            return measures[charge.measure] / charge.unit_size * charge.rate
        if isinstance(charge, PackageCharge):
            packages = measures[charge.measure] / charge.units
            rounding = {
                "ceil": ROUND_CEILING,
                "floor": ROUND_FLOOR,
                "nearest": ROUND_HALF_UP,
            }[charge.rounding]
            return packages.to_integral_value(rounding=rounding) * charge.amount
        if isinstance(charge, GraduatedCharge):
            remaining = measures[charge.measure]
            previous = Decimal(0)
            total = Decimal(0)
            for tier in charge.tiers:
                units = remaining if tier.up_to is None else min(remaining, tier.up_to - previous)
                if units > 0:
                    total += units * tier.rate
                    remaining -= units
                if remaining <= 0:
                    break
                previous = tier.up_to if tier.up_to is not None else previous
            return total
        if isinstance(charge, VolumeCharge):
            value = measures[charge.measure]
            tier = next(
                (candidate for candidate in charge.tiers if candidate.up_to is None or value <= candidate.up_to),
                charge.tiers[-1],
            )
            return value * tier.rate
        if isinstance(charge, ExpressionCharge):
            return evaluate_expression(charge.formula, measures)
        if isinstance(charge, SumCharge):
            return sum(
                (self._evaluate_charge(component, measures) for component in charge.components),
                start=Decimal(0),
            )
        raise AssertionError(f"unsupported charge type: {type(charge).__name__}")
