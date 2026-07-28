"""Pricing config parser — mirrors JS SDK's ``config/parse-pricing.ts``."""

from __future__ import annotations

from bursar.config.parse_utils import _charge_measure_names, _expression_charges
from bursar.config.types import ChargeUnmatched, PricingConfig, _validate_map_keys
from bursar.expr import ExpressionError, validate_expression


def _validate_pricing(pricing: PricingConfig) -> PricingConfig:  # noqa: C901
    if not pricing.operations:
        raise ValueError("pricing.operations must not be empty")
    if not pricing.rate_cards:
        raise ValueError("pricing.rate_cards must not be empty")
    _validate_map_keys(pricing.operations, "pricing.operations")
    _validate_map_keys(pricing.rate_cards, "pricing.rate_cards")

    for card_key, card in pricing.rate_cards.items():
        if card.extends is not None and card.extends not in pricing.rate_cards:
            raise ValueError(f"pricing.rate_cards.{card_key}.extends references unknown rate card '{card.extends}'")
        for operation, operation_price in card.operations.items():
            if operation not in pricing.operations:
                raise ValueError(f"pricing.rate_cards.{card_key}.operations references unknown operation '{operation}'")
            definition = pricing.operations[operation]
            charges = [rule.charge for rule in operation_price.rules]
            if isinstance(operation_price.unmatched, ChargeUnmatched):
                charges.append(operation_price.unmatched.charge)
            for index, rule in enumerate(operation_price.rules):
                unknown_dimensions = set(rule.when) - set(definition.dimensions)
                if unknown_dimensions:
                    raise ValueError(
                        f"pricing.rate_cards.{card_key}.operations.{operation}.rules[{index}] "
                        f"matches undeclared dimensions {sorted(unknown_dimensions)}"
                    )
            for charge in charges:
                unknown_measures = _charge_measure_names(charge) - set(definition.measures)
                if unknown_measures:
                    raise ValueError(
                        f"pricing for operation '{operation}' references undeclared measures {sorted(unknown_measures)}"
                    )
                for expression in _expression_charges(charge):
                    try:
                        validate_expression(expression.formula, known_variables=set(definition.measures))
                    except ExpressionError as exc:
                        raise ValueError(f"invalid expression charge for operation '{operation}': {exc}") from exc

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError(f"pricing rate-card inheritance cycle includes '{key}'")
        if key in visited:
            return
        visiting.add(key)
        parent = pricing.rate_cards[key].extends
        if parent is not None:
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in pricing.rate_cards:
        visit(key)

    return pricing
