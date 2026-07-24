"""Configuration hardening cases shared by storage-facing tests."""

import pytest

from bursar.config import ConfigError, canonical_bursar_config_dict, load_config_from_dict
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics


def config() -> dict:
    return {
        "version": 1,
        "usage": {
            "operations": {"completion": {"measures": ["tokens"], "dimensions": ["model"]}},
            "rate_cards": {
                "standard": {"prices": {"completion": [{"default": True, "formula": "tokens"}]}},
                "pro": {
                    "extends": "standard",
                    "prices": {"completion": [{"default": True, "formula": "tokens * 0.5"}]},
                },
            },
        },
        "credits": {"buckets": {"purchased": {}}, "spend_order": ["purchased"], "default_bucket": "purchased"},
        "plans": {"pro": {"display_name": "Pro", "rate_card": "pro"}},
    }


def test_signup_grant_requires_a_declared_bucket() -> None:
    data = config()
    data["credits"]["signup_grant"] = {"amount": 1, "bucket": "gifted"}
    with pytest.raises(ConfigError, match="configured bucket"):
        load_config_from_dict(data)


def test_plan_rate_card_requires_a_declared_card() -> None:
    data = config()
    data["plans"]["pro"]["rate_card"] = "missing"
    with pytest.raises(ConfigError, match="unknown rate card"):
        load_config_from_dict(data)


def test_catalog_config_is_the_canonical_public_document() -> None:
    stored = canonical_bursar_config_dict(config())
    assert stored["plans"]["pro"]["rate_card"] == "pro"
    assert "_bursar_public_config" not in stored
    assert "metering" not in stored
    assert "ledger" not in stored


def test_usage_metrics_require_an_explicit_operation() -> None:
    with pytest.raises(Exception, match="operation"):
        UsageMetrics()


def test_plan_rate_card_is_applied() -> None:
    engine = PricingEngine.from_dict(config())
    result = engine.calculate(
        UsageMetrics(operation="completion", measures={"tokens": 8}, dimensions={"model": "x"}),
        rate_card=engine.get_rate_card_for_plan("pro"),
    )
    assert str(result.total) == "4.0000"
