"""Pricing config parser — mirrors JS SDK's ``config/parse-pricing.ts``."""

from __future__ import annotations

from decimal import Decimal

from bursar.config.parse_utils import _charge_measure_names, _expression_charges
from bursar.config.types import (
    ChargeUnmatched,
    DimensionDefinition,
    EqualMatcher,
    InMatcher,
    MatcherScalar,
    NotInMatcher,
    OperationDefinition,
    OperationPricing,
    PrefixMatcher,
    PricingConfig,
    RangeMatcher,
    _parse_decimal_string,
    _validate_map_keys,
)
from bursar.expr import ExpressionError, validate_expression


def _validate_matcher(
    matcher: EqualMatcher | InMatcher | NotInMatcher | PrefixMatcher | RangeMatcher,
    dimension: DimensionDefinition,
    path: str,
) -> None:
    if isinstance(matcher, PrefixMatcher):
        if dimension.type != "string":
            raise ValueError(f"{path} prefix matcher requires a string dimension")
        return
    if isinstance(matcher, RangeMatcher):
        if dimension.type != "number":
            raise ValueError(f"{path} range matcher requires a number dimension")
        return

    values = [matcher.value] if isinstance(matcher, EqualMatcher) else list(matcher.values)
    if dimension.type == "string":
        if any(not isinstance(value, str) for value in values):
            raise ValueError(f"{path} matcher values must be strings")
        return
    if dimension.type == "boolean":
        if any(type(value) is not bool for value in values):
            raise ValueError(f"{path} matcher values must be booleans")
        return

    normalized: list[MatcherScalar] = []
    for value in values:
        if type(value) is bool:
            raise ValueError(f"{path} matcher values must be decimal strings or numbers")
        if isinstance(value, Decimal):
            normalized.append(value)
        elif isinstance(value, str):
            normalized.append(_parse_decimal_string(value))
        else:  # pragma: no cover - Pydantic normalizes JSON numbers to Decimal
            normalized.append(Decimal(value))
    if isinstance(matcher, EqualMatcher):
        matcher.value = normalized[0]
    else:
        matcher.values = normalized


def _validate_price_rules(
    card_key: str,
    operation: str,
    operation_price: OperationPricing,
    definition: OperationDefinition,
) -> None:
    for index, rule in enumerate(operation_price.rules):
        unknown_dimensions = set(rule.when) - set(definition.dimensions)
        if unknown_dimensions:
            raise ValueError(
                f"pricing.rate_cards.{card_key}.operations.{operation}.rules[{index}] "
                f"matches undeclared dimensions {sorted(unknown_dimensions)}"
            )
        for dimension_key, matcher in rule.when.items():
            _validate_matcher(
                matcher,
                definition.dimensions[dimension_key],
                f"pricing.rate_cards.{card_key}.operations.{operation}.rules[{index}].when.{dimension_key}",
            )


def _validate_price_charges(
    operation: str,
    operation_price: OperationPricing,
    definition: OperationDefinition,
) -> None:
    charges = [rule.charge for rule in operation_price.rules]
    if isinstance(operation_price.unmatched, ChargeUnmatched):
        charges.append(operation_price.unmatched.charge)
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


def _validate_rate_cards(pricing: PricingConfig) -> None:
    for card_key, card in pricing.rate_cards.items():
        if card.extends is not None and card.extends not in pricing.rate_cards:
            raise ValueError(f"pricing.rate_cards.{card_key}.extends references unknown rate card '{card.extends}'")
        for operation, operation_price in card.operations.items():
            definition = pricing.operations.get(operation)
            if definition is None:
                raise ValueError(f"pricing.rate_cards.{card_key}.operations references unknown operation '{operation}'")
            _validate_price_rules(card_key, operation, operation_price, definition)
            _validate_price_charges(operation, operation_price, definition)


def _validate_rate_card_inheritance(pricing: PricingConfig) -> None:
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


def _validate_pricing(pricing: PricingConfig) -> PricingConfig:
    if not pricing.operations:
        raise ValueError("pricing.operations must not be empty")
    if not pricing.rate_cards:
        raise ValueError("pricing.rate_cards must not be empty")
    _validate_map_keys(pricing.operations, "pricing.operations")
    _validate_map_keys(pricing.rate_cards, "pricing.rate_cards")
    _validate_rate_cards(pricing)
    _validate_rate_card_inheritance(pricing)

    return pricing
