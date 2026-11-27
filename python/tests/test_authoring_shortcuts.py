from copy import deepcopy
from decimal import Decimal

import pytest

from bursar.config import ConfigError, canonical_bursar_config_dict
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics


def catalog() -> dict:
    return {
        "version": 1,
        "catalog": {"default_plan": "free"},
        "credits": {"on_signup": None},
        "pricing": {"operations": {"report": {"charge": {"type": "flat", "amount": "2"}}}},
        "plans": {"free": {"display_name": "Free", "allowed_operations": ["report"]}},
    }


def test_single_nonstandard_card_is_inferred_and_inline_charge_fills_it() -> None:
    raw = catalog()
    raw["pricing"]["rate_cards"] = {"custom": {"operations": {}}}
    compiled = canonical_bursar_config_dict(raw)
    assert compiled["plans"]["free"]["rate_card"] == "custom"
    assert PricingEngine.from_dict(compiled).calculate(UsageMetrics(operation="report")).total == Decimal(2)
    assert canonical_bursar_config_dict(compiled) == compiled


def test_inline_is_base_for_roots_and_explicit_inherited_price_wins() -> None:
    raw = catalog()
    raw["pricing"]["rate_cards"] = {
        "basic": {
            "operations": {"report": {"unmatched": {"action": "charge", "charge": {"type": "flat", "amount": "3"}}}}
        },
        "premium": {"extends": "basic"},
        "other": {"operations": {}},
    }
    with pytest.raises(ConfigError):
        canonical_bursar_config_dict(raw)
    raw["plans"]["free"]["rate_card"] = "premium"
    config = canonical_bursar_config_dict(raw)
    engine = PricingEngine.from_dict(config)
    assert engine.calculate(UsageMetrics(operation="report"), rate_card="premium").total == Decimal(3)
    assert engine.calculate(UsageMetrics(operation="report"), rate_card="other").total == Decimal(2)
    assert canonical_bursar_config_dict(config) == config


def test_null_charge_means_absent_and_unknown_provider_fields_are_rejected() -> None:
    raw = catalog()
    raw["pricing"]["operations"]["report"]["charge"] = None
    raw["pricing"]["rate_cards"] = {
        "only": {
            "operations": {"report": {"unmatched": {"action": "charge", "charge": {"type": "flat", "amount": "4"}}}}
        }
    }
    assert canonical_bursar_config_dict(raw)["plans"]["free"]["rate_card"] == "only"
    assert "signup" not in canonical_bursar_config_dict(raw)["credits"]["grant_programs"]
    bad = deepcopy(raw)
    bad["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "pack": {
                "type": "topup",
                "display_name": "Pack",
                "price": {"amount_minor": 100, "currency": "USD"},
                "credits_per_unit": "10",
                "bucket": "default",
                "providers": {"stripe": {"product_id": "wrong"}},
            }
        },
    }
    with pytest.raises(ConfigError):
        canonical_bursar_config_dict(bad)


def test_declared_provider_infers_reference_kind_and_rejects_conflicts() -> None:
    raw = catalog()
    raw["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}, "dodo": {"type": "dodo"}},
        "offers": {
            "stripe_pack": {
                "type": "topup",
                "display_name": "Stripe",
                "price": {"amount_minor": 100, "currency": "USD"},
                "credits_per_unit": "10",
                "bucket": "default",
                "providers": {"stripe": {"price_id": "stripe_price"}},
            },
            "dodo_pack": {
                "type": "topup",
                "display_name": "Dodo",
                "price": {"amount_minor": 100, "currency": "USD"},
                "credits_per_unit": "10",
                "bucket": "default",
                "providers": {"dodo": {"product_id": "dodo_product"}},
            },
        },
    }
    result = canonical_bursar_config_dict(raw)
    offers = result["commerce"]["offers"]
    assert offers["stripe_pack"]["providers"]["stripe"]["type"] == "stripe_price"
    assert offers["dodo_pack"]["providers"]["dodo"]["type"] == "dodo_product"
    assert canonical_bursar_config_dict(result) == result
    raw["commerce"]["offers"]["stripe_pack"]["providers"]["stripe"]["type"] = "dodo_product"
    with pytest.raises(ConfigError):
        canonical_bursar_config_dict(raw)
