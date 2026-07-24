"""Generic operation pricing engine for the Bursar v1 configuration."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bursar.breakdown import CostBreakdown
from bursar.config import BursarConfig, ConfigError, PriceRule, load_config_from_dict
from bursar.expr import evaluate_expression
from bursar.metrics import METRIC_VARIABLES, UsageMetrics

__all__ = ["METRIC_VARIABLES", "PricingEngine"]

_QUANTUM = Decimal("0.0001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


class PricingEngine:
    """Calculate exact credit costs for configured usage operations."""

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
        # Balance admission is a plan spending policy in the redesigned schema.
        return Decimal(0)

    def calculate(self, metrics: UsageMetrics, *, rate_card: str | None = None) -> CostBreakdown:
        usage = self._config.usage
        if usage is None:
            raise ConfigError("usage pricing is not configured")
        operation = metrics.operation
        if operation not in usage.operations:
            raise ConfigError(f"unknown usage operation '{operation}'")

        definition = usage.operations[operation]
        measures = dict(metrics.measures)
        dimensions = dict(metrics.dimensions)
        unknown_measures = set(measures) - set(definition.measures)
        unknown_dimensions = set(dimensions) - set(definition.dimensions)
        if unknown_measures:
            raise ConfigError(f"operation '{operation}' received undeclared measures {sorted(unknown_measures)}")
        if unknown_dimensions:
            raise ConfigError(f"operation '{operation}' received undeclared dimensions {sorted(unknown_dimensions)}")

        card_key = self._resolve_rate_card(rate_card)
        rules = self._rules_for(card_key, operation)
        rule = next((candidate for candidate in rules if self._matches(candidate, dimensions)), None)
        if rule is None:
            raise ConfigError(f"no price rule matched operation '{operation}' in rate card '{card_key}'")

        variables = {measure: measures.get(measure, Decimal(0)) for measure in definition.measures}
        value = evaluate_expression(rule.formula, variables)
        if not value.is_finite() or value < 0:
            raise ConfigError(f"price formula for '{operation}' produced a negative or non-finite credit cost")
        total = _q(value)
        return CostBreakdown(
            operation_credits=total,
            total=total,
            breakdown={
                "operation": operation,
                "rate_card": card_key,
                "measures": {key: str(value) for key, value in measures.items()},
                "dimensions": dimensions,
            },
        )

    def calculate_batch(self, metrics_list: list[UsageMetrics], *, rate_card: str | None = None) -> list[CostBreakdown]:
        return [self.calculate(metrics, rate_card=rate_card) for metrics in metrics_list]

    def get_rate_card_for_plan(self, plan_id: str | None) -> str | None:
        if plan_id is None:
            return None
        plan = self._config.plans.get(plan_id)
        return plan.rate_card if plan is not None else None

    def _resolve_rate_card(self, requested: str | None) -> str:
        assert self._config.usage is not None
        cards = self._config.usage.rate_cards
        if requested is not None:
            if requested not in cards:
                raise ConfigError(f"unknown rate card '{requested}'")
            return requested
        if len(cards) == 1:
            return next(iter(cards))
        raise ConfigError("rate_card is required when more than one rate card is configured")

    def _rules_for(self, rate_card: str, operation: str) -> list[PriceRule]:
        assert self._config.usage is not None
        card = self._config.usage.rate_cards[rate_card]
        if operation in card.prices:
            return card.prices[operation]
        if card.extends is None:
            raise ConfigError(f"rate card '{rate_card}' has no price for operation '{operation}'")
        return self._rules_for(card.extends, operation)

    @staticmethod
    def _matches(rule: PriceRule, dimensions: dict[str, str]) -> bool:
        if rule.default:
            return True
        assert rule.match is not None
        for key, matcher in rule.match.items():
            value = dimensions.get(key)
            if value is None:
                return False
            if matcher.exact is not None and value != matcher.exact:
                return False
            if matcher.prefix is not None and not value.startswith(matcher.prefix):
                return False
        return True
