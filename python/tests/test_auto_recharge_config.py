from bursar.config import BursarConfig


def test_auto_recharge_config_is_mirrored_in_python() -> None:
    config = BursarConfig(
        metering={"models": {"*": "input_tokens + output_tokens"}},
        billing={
            "topups": {"default": {"deposit_to": "purchased"}},
            "auto_recharge": {
                "enabled": True,
                "threshold_credits": 5000,
                "topup_key": "default",
                "quantity": 1,
                "max_recharges": 3,
                "window_days": 30,
            },
        },
    )
    assert config.billing is not None
    assert config.billing.auto_recharge is not None
    assert config.billing.auto_recharge.threshold_credits == 5000
    assert config.billing.auto_recharge.topup_key == "default"
