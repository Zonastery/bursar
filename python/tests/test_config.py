from decimal import Decimal

import pytest

from bursar import project_public_catalog
from bursar.config import ConfigError, EqualMatcher, canonical_bursar_config_dict, load_config_from_dict
from bursar.config.types import CreditLinePolicy


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
                },
                "image": {
                    "measures": {"images": {"unit": "image"}},
                    "dimensions": {},
                },
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
                                        "rate": "2.000000",
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
                                            "rate": "1.000000",
                                        },
                                        {
                                            "type": "per_unit",
                                            "measure": "output_tokens",
                                            "rate": "1.000000",
                                        },
                                    ],
                                },
                            },
                        }
                    }
                }
            },
        },
        "credits": {
            "accounting": {"unit": "credit", "scale": 6, "rounding": "half_up"},
            "buckets": {
                "gifted": {
                    "priority": 10,
                    "expiry": {
                        "type": "after_grant",
                        "interval": {"unit": "day", "count": 7},
                        "timezone": "UTC",
                    },
                },
                "purchased": {"priority": 20},
            },
            "default_bucket": "purchased",
            "policies": {
                "prepaid": {"type": "prepaid"},
                "invoice": {"type": "credit_line", "limit": "500.000000"},
            },
        },
        "entitlements": {
            "features": {
                "tutor_chat": {"type": "boolean", "default": False},
                "access_level": {
                    "type": "enum",
                    "values": ["basic", "full"],
                    "default": "basic",
                },
            }
        },
        "admission": {
            "policies": {
                "interactive": {
                    "max_in_flight": 5,
                    "operations": {"completion": {"max_in_flight": 2}},
                }
            }
        },
        "plans": {
            "pro": {
                "display_name": "Pro",
                "rank": 0,
                "rate_card": "standard",
                "allowed_operations": ["completion"],
                "features": {"tutor_chat": True, "access_level": "full"},
                "credit_allowance": {
                    "amount": "10.000000",
                    "window": {
                        "type": "calendar",
                        "unit": "month",
                        "timezone": "UTC",
                    },
                },
                "quotas": {
                    "token_budget": {
                        "operation": "completion",
                        "measure": "input_tokens",
                        "limit": "1000.000000",
                        "window": {
                            "type": "rolling",
                            "duration": {"unit": "day", "count": 30},
                        },
                        "enforcement": "block",
                        "emit_at_percent": [80, 100],
                    }
                },
                "credit_policy": "invoice",
                "admission_policy": "interactive",
            }
        },
    }


def test_removed_catalog_activation_is_rejected() -> None:
    config = base_config()
    config["catalog"] = {"activation": {"mode": "on_publish"}}

    with pytest.raises(ConfigError, match="activation"):
        load_config_from_dict(config)


def test_accepts_typed_catalog() -> None:
    config = load_config_from_dict(base_config())
    assert config.pricing is not None
    assert config.plans["pro"].credit_allowance is not None
    assert config.plans["pro"].credit_allowance.amount == Decimal("10")
    assert config.plans["pro"].revision_policy == "immediate"


def test_fixed_accounting_and_plan_rank_have_authoring_defaults() -> None:
    data = base_config()
    data["credits"].pop("accounting")
    data["plans"]["pro"].pop("rank")

    config = load_config_from_dict(data)

    assert config.credits.accounting.unit == "credit"
    assert config.credits.accounting.scale == 6
    assert config.plans["pro"].rank == 0


def test_canonicalizes_credit_decimals_to_six_places() -> None:
    canonical = canonical_bursar_config_dict(base_config())
    assert canonical["credits"]["policies"]["invoice"]["limit"] == "500.000000"
    assert canonical["plans"]["pro"]["credit_allowance"]["amount"] == "10.000000"


def test_rate_cards_may_be_partial_for_unused_operations() -> None:
    config = load_config_from_dict(base_config())
    assert config.pricing is not None
    assert "image" not in config.pricing.rate_cards["standard"].operations


def test_enabled_operation_requires_pricing() -> None:
    data = base_config()
    data["plans"]["pro"]["allowed_operations"].append("image")
    with pytest.raises(ConfigError, match="without pricing"):
        load_config_from_dict(data)


def test_charge_measure_must_exist() -> None:
    data = base_config()
    data["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["unmatched"]["charge"] = {
        "type": "per_unit",
        "measure": "typo_tokens",
        "rate": "1.000000",
    }
    with pytest.raises(ConfigError, match="undeclared measures"):
        load_config_from_dict(data)


def test_feature_values_are_typed_and_referenced() -> None:
    data = base_config()
    data["plans"]["pro"]["features"]["access_level"] = "enterprise"
    with pytest.raises(ConfigError, match="must be one of"):
        load_config_from_dict(data)


def test_feature_values_enforce_declared_types_and_constraints() -> None:
    data = base_config()
    data["entitlements"]["features"].update(
        {
            "agent_limit": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
                "maximum": 10,
            },
            "support_tier": {
                "type": "string",
                "default": "standard",
                "pattern": "^(standard|priority)$",
            },
        }
    )
    data["plans"]["pro"]["features"].update(
        {
            "tutor_chat": "yes",
            "agent_limit": 99,
            "support_tier": "unknown",
        }
    )

    with pytest.raises(ConfigError, match="tutor_chat.*boolean"):
        load_config_from_dict(data)

    data["plans"]["pro"]["features"]["tutor_chat"] = True
    with pytest.raises(ConfigError, match="agent_limit.*maximum"):
        load_config_from_dict(data)

    data["plans"]["pro"]["features"]["agent_limit"] = 5
    with pytest.raises(ConfigError, match="support_tier.*pattern"):
        load_config_from_dict(data)


def test_quota_references_declared_operation_measure() -> None:
    data = base_config()
    data["plans"]["pro"]["quotas"]["token_budget"]["measure"] = "calls"
    with pytest.raises(ConfigError, match="unknown measure"):
        load_config_from_dict(data)


def test_credit_line_does_not_require_overdraft_bucket() -> None:
    config = load_config_from_dict(base_config())
    policy = config.credits.policies["invoice"]
    assert isinstance(policy, CreditLinePolicy)
    assert policy.limit == Decimal("500")


def test_duplicate_bucket_priorities_are_rejected() -> None:
    data = base_config()
    data["credits"]["buckets"]["purchased"]["priority"] = 10
    with pytest.raises(ConfigError, match="priorities"):
        load_config_from_dict(data)


def test_default_bucket_and_plan_policy_references_are_validated() -> None:
    data = base_config()
    data["credits"]["default_bucket"] = "typo"
    with pytest.raises(ConfigError, match="default_bucket"):
        load_config_from_dict(data)

    data = base_config()
    data["plans"]["pro"]["credit_policy"] = "typo"
    with pytest.raises(ConfigError, match="credit_policy"):
        load_config_from_dict(data)

    data = base_config()
    data["plans"]["pro"]["admission_policy"] = "typo"
    with pytest.raises(ConfigError, match="admission_policy"):
        load_config_from_dict(data)


def test_credit_allowance_requires_a_default_bucket() -> None:
    data = base_config()
    data["credits"].pop("default_bucket")
    with pytest.raises(ConfigError, match="credit_allowance requires credits.default_bucket"):
        load_config_from_dict(data)


def test_matcher_operator_must_match_dimension_type() -> None:
    data = base_config()
    data["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["rules"][0]["when"]["model"] = {
        "op": "range",
        "gte": "1",
    }
    with pytest.raises(ConfigError, match="range matcher requires a number dimension"):
        load_config_from_dict(data)


def test_numeric_matcher_decimal_strings_are_normalized() -> None:
    data = base_config()
    data["pricing"]["operations"]["completion"]["dimensions"]["model"]["type"] = "number"
    data["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["rules"][0]["when"]["model"] = {
        "op": "eq",
        "value": "1.5",
    }

    config = load_config_from_dict(data)
    matcher = config.pricing.rate_cards["standard"].operations["completion"].rules[0].when["model"]

    assert isinstance(matcher, EqualMatcher)
    assert matcher.value == Decimal("1.5")


def test_provider_references_are_declared_and_compatible() -> None:
    data = base_config()
    data["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "pro_monthly": {
                "type": "subscription",
                "display_name": "Pro monthly",
                "price": {"amount_minor": 1200, "currency": "USD"},
                "providers": {
                    "stripe": {
                        "type": "dodo_product",
                        "product_id": "product_wrong_provider",
                    }
                },
                "plan": "pro",
                "billing_interval": {"unit": "month"},
            }
        },
    }

    with pytest.raises(ConfigError, match="incompatible provider reference"):
        load_config_from_dict(data)


def test_subscription_defaults_to_next_renewal_and_uses_typed_provider_ref() -> None:
    data = base_config()
    data["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "pro_monthly": {
                "type": "subscription",
                "display_name": "Pro monthly",
                "price": {
                    "amount_minor": 1200,
                    "currency": "USD",
                    "tax_behavior": "exclusive",
                },
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_pro_monthly"}},
                "plan": "pro",
                "billing_interval": {"unit": "month"},
            }
        },
    }
    config = load_config_from_dict(data)
    assert config.plans["pro"].revision_policy == "next_renewal"


def test_public_catalog_preserves_prices_without_provider_identifiers() -> None:
    data = base_config()
    data["catalog"] = {"default_plan": "pro"}
    data["credits"]["display"] = {"currency": "USD", "units_per_major": "1000"}
    data["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "pro_monthly": {
                "type": "subscription",
                "display_name": "Pro monthly",
                "price": {
                    "amount_minor": 1200,
                    "currency": "USD",
                    "tax_behavior": "exclusive",
                },
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_secret_pro_monthly",
                    }
                },
                "plan": "pro",
                "billing_interval": {"unit": "month"},
            }
        },
    }

    public = project_public_catalog(load_config_from_dict(data))

    assert public["default_plan"] == "pro"
    assert public["credit_display"] == {"currency": "USD", "units_per_major": "1000"}
    assert public["plans"][0]["offers"][0]["price"]["amount_minor"] == 1200
    assert "price_secret_pro_monthly" not in str(public)


def test_missing_version_is_rejected() -> None:
    data = base_config()
    data.pop("version")
    with pytest.raises(ConfigError):
        load_config_from_dict(data)
