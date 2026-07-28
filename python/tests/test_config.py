from decimal import Decimal

import pytest

from bursar.config import ConfigError, canonical_bursar_config_dict, load_config_from_dict


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


def test_accepts_typed_catalog() -> None:
    config = load_config_from_dict(base_config())
    assert config.pricing is not None
    assert config.plans["pro"].credit_allowance is not None
    assert config.plans["pro"].credit_allowance.amount == Decimal("10")
    assert config.plans["pro"].revision_policy == "immediate"


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


def test_quota_references_declared_operation_measure() -> None:
    data = base_config()
    data["plans"]["pro"]["quotas"]["token_budget"]["measure"] = "calls"
    with pytest.raises(ConfigError, match="unknown measure"):
        load_config_from_dict(data)


def test_credit_line_does_not_require_overdraft_bucket() -> None:
    config = load_config_from_dict(base_config())
    assert config.credits.policies["invoice"].limit == Decimal("500")


def test_duplicate_bucket_priorities_are_rejected() -> None:
    data = base_config()
    data["credits"]["buckets"]["purchased"]["priority"] = 10
    with pytest.raises(ConfigError, match="priorities"):
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


def test_missing_version_is_rejected() -> None:
    data = base_config()
    data.pop("version")
    with pytest.raises(ConfigError):
        load_config_from_dict(data)
