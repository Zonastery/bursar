import pytest

from bursar.config import ConfigError, load_config_from_dict


def test_auto_recharge_config_is_guardrails_not_forced_activation() -> None:
    config = load_config_from_dict(
        {
            "version": 1,
            "credits": {
                "buckets": {"purchased": {"priority": 10}},
                "default_bucket": "purchased",
            },
            "commerce": {
                "providers": {"stripe": {"type": "stripe"}},
                "offers": {
                    "small_pack": {
                        "type": "topup",
                        "display_name": "1,000 credits",
                        "price": {"amount_minor": 500, "currency": "USD"},
                        "providers": {
                            "stripe": {
                                "type": "stripe_price",
                                "price_id": "price_pack",
                            }
                        },
                        "credits_per_unit": "1000.000000",
                        "quantity": {"minimum": 1, "maximum": 3, "default": 1},
                        "bucket": "purchased",
                    }
                },
                "auto_recharge": {
                    "eligible_topups": ["small_pack"],
                    "balance_below": {
                        "minimum": "100.000000",
                        "maximum": "5000.000000",
                        "default": "1000.000000",
                    },
                    "rearm_above": "6000.000000",
                    "quantity": {"minimum": 1, "maximum": 3, "default": 1},
                    "limits": {
                        "max_purchases": 3,
                        "window": {
                            "type": "rolling",
                            "duration": {"unit": "day", "count": 30},
                        },
                        "max_charge_minor": 1500,
                        "cooldown": {"unit": "hour", "count": 1},
                    },
                },
            },
        }
    )
    assert config.commerce.auto_recharge is not None
    assert config.commerce.auto_recharge.balance_below.default == 1000


def test_auto_recharge_rearm_and_quantity_ranges_are_cross_validated() -> None:
    data = {
        "version": 1,
        "credits": {
            "buckets": {"purchased": {"priority": 10}},
            "default_bucket": "purchased",
        },
        "commerce": {
            "providers": {"stripe": {"type": "stripe"}},
            "offers": {
                "small_pack": {
                    "type": "topup",
                    "display_name": "1,000 credits",
                    "price": {"amount_minor": 500, "currency": "USD"},
                    "providers": {
                        "stripe": {
                            "type": "stripe_price",
                            "price_id": "price_pack",
                        }
                    },
                    "credits_per_unit": "1000",
                    "quantity": {"minimum": 1, "maximum": 3, "default": 1},
                    "bucket": "purchased",
                }
            },
            "auto_recharge": {
                "eligible_topups": ["small_pack"],
                "balance_below": {
                    "minimum": "100",
                    "maximum": "5000",
                    "default": "1000",
                },
                "rearm_above": "500",
                "quantity": {"minimum": 1, "maximum": 3, "default": 1},
                "limits": {
                    "max_purchases": 3,
                    "window": {
                        "type": "rolling",
                        "duration": {"unit": "day", "count": 30},
                    },
                    "max_charge_minor": 1500,
                    "cooldown": {"unit": "hour", "count": 1},
                },
            },
        },
    }

    with pytest.raises(ConfigError, match="rearm_above"):
        load_config_from_dict(data)

    data["commerce"]["auto_recharge"]["rearm_above"] = "6000"
    data["commerce"]["auto_recharge"]["quantity"]["maximum"] = 4
    with pytest.raises(ConfigError, match="quantity must fit"):
        load_config_from_dict(data)
