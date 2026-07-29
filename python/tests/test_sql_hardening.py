"""Configuration hardening cases shared by storage-facing tests."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bursar.config import (
    ConfigError,
    canonical_bursar_config_dict,
    load_config_from_dict,
)
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics


def config() -> dict:
    return {
        "version": 1,
        "pricing": {
            "operations": {
                "completion": {
                    "measures": {"tokens": {"unit": "token"}},
                    "dimensions": {"model": {"type": "string"}},
                }
            },
            "rate_cards": {
                "standard": {
                    "operations": {
                        "completion": {
                            "rules": [],
                            "unmatched": {
                                "action": "charge",
                                "charge": {
                                    "type": "per_unit",
                                    "measure": "tokens",
                                    "rate": "1",
                                },
                            },
                        }
                    }
                },
                "pro": {
                    "extends": "standard",
                    "operations": {
                        "completion": {
                            "rules": [],
                            "unmatched": {
                                "action": "charge",
                                "charge": {
                                    "type": "per_unit",
                                    "measure": "tokens",
                                    "rate": "0.5",
                                },
                            },
                        }
                    },
                },
            },
        },
        "credits": {
            "accounting": {
                "unit": "credit",
                "scale": 6,
                "rounding": "half_up",
            },
            "buckets": {
                "purchased": {
                    "priority": 10,
                    "expiry": {"type": "never"},
                }
            },
            "default_bucket": "purchased",
        },
        "plans": {
            "pro": {
                "display_name": "Pro",
                "rank": 0,
                "rate_card": "pro",
            }
        },
    }


def test_grant_program_requires_a_declared_bucket() -> None:
    data = config()
    data["credits"]["grant_programs"] = {
        "welcome": {
            "trigger": "account_created",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "1",
                    "bucket": "gifted",
                }
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "subject",
        }
    }

    with pytest.raises(ConfigError, match="unknown bucket"):
        load_config_from_dict(data)


def test_plan_rate_card_requires_a_declared_card() -> None:
    data = config()
    data["plans"]["pro"]["rate_card"] = "missing"

    with pytest.raises(ConfigError, match="unknown rate card"):
        load_config_from_dict(data)


def test_catalog_config_is_the_canonical_public_document() -> None:
    stored = canonical_bursar_config_dict(config())

    assert stored["plans"]["pro"]["rate_card"] == "pro"
    assert stored["credits"]["accounting"] == {
        "unit": "credit",
        "scale": 6,
        "rounding": "half_up",
    }


def test_usage_metrics_require_an_operation() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        UsageMetrics()  # type: ignore[call-arg]


def test_typed_rate_card_is_used_by_the_pricing_engine() -> None:
    engine = PricingEngine.from_dict(config())
    result = engine.calculate(
        UsageMetrics(
            operation="completion",
            measures={"tokens": 8},
            dimensions={"model": "test"},
        ),
        rate_card=engine.get_rate_card_for_plan("pro"),
    )

    assert result.total == Decimal("4.000000")
