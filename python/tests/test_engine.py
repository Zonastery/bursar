from decimal import Decimal

import pytest

from bursar.config import ConfigError
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics


def pricing() -> dict:
    return {
        "version": 1,
        "usage": {
            "operations": {
                "completion": {"measures": ["input_tokens", "output_tokens"], "dimensions": ["model"]},
                "image": {"measures": ["images"], "dimensions": []},
            },
            "rate_cards": {
                "standard": {
                    "prices": {
                        "completion": [
                            {"match": {"model": {"exact": "fast"}}, "formula": "input_tokens * 2"},
                            {
                                "match": {"model": {"prefix": "premium-"}},
                                "formula": "input_tokens * 3 + output_tokens * 4",
                            },
                            {"default": True, "formula": "input_tokens + output_tokens"},
                        ],
                        "image": [{"default": True, "formula": "images * 5"}],
                    }
                },
                "discount": {
                    "extends": "standard",
                    "prices": {"completion": [{"default": True, "formula": "input_tokens * 0.5 + output_tokens"}]},
                },
            },
        },
        "plans": {"pro": {"display_name": "Pro", "rate_card": "discount"}},
    }


def test_exact_match_wins_before_default() -> None:
    engine = PricingEngine.from_dict(pricing())
    result = engine.calculate(
        UsageMetrics(operation="completion", measures={"input_tokens": 2}, dimensions={"model": "fast"}),
        rate_card="standard",
    )
    assert result.total == Decimal("4.0000")


def test_explicit_prefix_match() -> None:
    engine = PricingEngine.from_dict(pricing())
    result = engine.calculate(
        UsageMetrics(
            operation="completion", measures={"input_tokens": 2, "output_tokens": 1}, dimensions={"model": "premium-x"}
        ),
        rate_card="standard",
    )
    assert result.total == Decimal("10.0000")


def test_inherited_rate_card_replaces_complete_operation_rules() -> None:
    engine = PricingEngine.from_dict(pricing())
    result = engine.calculate(
        UsageMetrics(
            operation="completion", measures={"input_tokens": 2, "output_tokens": 1}, dimensions={"model": "fast"}
        ),
        rate_card="discount",
    )
    assert result.total == Decimal("2.0000")


def test_inherited_rate_card_keeps_other_operations() -> None:
    engine = PricingEngine.from_dict(pricing())
    assert engine.calculate(
        UsageMetrics(operation="image", measures={"images": 2}), rate_card="discount"
    ).total == Decimal("10.0000")


def test_unknown_usage_is_rejected() -> None:
    engine = PricingEngine.from_dict(pricing())
    with pytest.raises(ConfigError, match="unknown usage operation"):
        engine.calculate(UsageMetrics(operation="audio"), rate_card="standard")


def test_undeclared_measure_is_rejected() -> None:
    engine = PricingEngine.from_dict(pricing())
    with pytest.raises(ConfigError, match="undeclared measures"):
        engine.calculate(UsageMetrics(operation="image", measures={"seconds": 2}), rate_card="standard")


def test_multiple_rate_cards_require_selection() -> None:
    engine = PricingEngine.from_dict(pricing())
    with pytest.raises(ConfigError, match="rate_card is required"):
        engine.calculate(UsageMetrics(operation="image", measures={"images": 1}))
