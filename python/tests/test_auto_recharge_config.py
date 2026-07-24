from bursar.config import BursarConfig


def test_auto_recharge_config_has_one_policy_shape() -> None:
    config = BursarConfig(
        credits={
            "buckets": {"purchased": {}},
            "spend_order": ["purchased"],
            "default_bucket": "purchased",
        },
        payments={
            "topups": {
                "small_pack": {
                    "credits": 1000,
                    "bucket": "purchased",
                    "providers": {"stripe": {"lookup": {"type": "price_id", "value": "price_pack"}}},
                }
            },
            "auto_recharge": {
                "trigger": {"balance_below": 5000},
                "purchase": {"topup": "small_pack", "quantity": 1},
                "limit": {"max_purchases": 3, "period": {"unit": "day", "count": 30, "anchor": "rolling"}},
            },
        },
    )
    assert config.payments is not None
    assert config.payments.auto_recharge is not None
    assert config.payments.auto_recharge.trigger.balance_below == 5000
