from decimal import Decimal

import pytest

from bursar.config import ConfigError
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics


def base_config() -> dict:
    return {
        "version": 1,
        "pricing": {
            "operations": {
                "completion": {
                    "measures": {
                        "input_tokens": {"unit": "token"},
                        "output_tokens": {"unit": "token"},
                    },
                    "dimensions": {"model": {"type": "string"}},
                }
            },
            "rate_cards": {
                "standard": {
                    "operations": {
                        "completion": {
                            "rules": [
                                {
                                    "when": {"model": {"op": "prefix", "value": "gpt-"}},
                                    "charge": {
                                        "type": "per_unit",
                                        "measure": "input_tokens",
                                        "rate": "2",
                                    },
                                }
                            ],
                            "unmatched": {
                                "action": "charge",
                                "charge": {
                                    "type": "sum",
                                    "components": [
                                        {
                                            "type": "per_unit",
                                            "measure": "input_tokens",
                                            "rate": "1",
                                        },
                                        {
                                            "type": "per_unit",
                                            "measure": "output_tokens",
                                            "rate": "1",
                                        },
                                    ],
                                },
                            },
                        }
                    }
                }
            },
        },
        "credits": {},
    }


def test_typed_match_rule_wins() -> None:
    engine = PricingEngine.from_dict(base_config())
    result = engine.calculate(
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": Decimal("2")},
            dimensions={"model": "gpt-fast"},
        )
    )
    assert result.total == Decimal("4.000000")


def test_unmatched_charge_is_explicit() -> None:
    engine = PricingEngine.from_dict(base_config())
    result = engine.calculate(
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": Decimal("2"), "output_tokens": Decimal("1")},
            dimensions={"model": "other"},
        )
    )
    assert result.total == Decimal("3.000000")


def test_unmatched_rejects_when_configured() -> None:
    config = base_config()
    config["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["unmatched"] = {"action": "reject"}
    engine = PricingEngine.from_dict(config)
    with pytest.raises(ConfigError, match="no price rule"):
        engine.calculate(
            UsageMetrics(
                operation="completion",
                measures={"input_tokens": Decimal("1")},
                dimensions={"model": "other"},
            )
        )


@pytest.mark.parametrize(
    ("charge", "measure", "expected"),
    [
        ({"type": "flat", "amount": "3.000000"}, Decimal("7"), Decimal("3.000000")),
        (
            {
                "type": "package",
                "measure": "input_tokens",
                "units": "10",
                "amount": "2",
                "rounding": "ceil",
            },
            Decimal("11"),
            Decimal("4.000000"),
        ),
        (
            {
                "type": "graduated",
                "measure": "input_tokens",
                "tiers": [
                    {"up_to": "10", "rate": "1"},
                    {"up_to": None, "rate": "2"},
                ],
            },
            Decimal("15"),
            Decimal("20.000000"),
        ),
        (
            {
                "type": "volume",
                "measure": "input_tokens",
                "tiers": [
                    {"up_to": "10", "rate": "2"},
                    {"up_to": None, "rate": "1"},
                ],
            },
            Decimal("15"),
            Decimal("15.000000"),
        ),
    ],
)
def test_typed_charge_rules(charge: dict, measure: Decimal, expected: Decimal) -> None:
    config = base_config()
    config["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["rules"] = []
    config["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["unmatched"] = {
        "action": "charge",
        "charge": charge,
    }
    engine = PricingEngine.from_dict(config)
    result = engine.calculate(
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": measure},
            dimensions={"model": "anything"},
        )
    )
    assert result.total == expected
