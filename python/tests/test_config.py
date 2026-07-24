from decimal import Decimal

import pytest

from bursar.config import ConfigError, canonical_bursar_config_dict, load_config_from_dict


def base_config() -> dict:
    return {
        "version": 1,
        "usage": {
            "operations": {
                "completion": {
                    "measures": ["input_tokens", "output_tokens"],
                    "dimensions": ["model"],
                }
            },
            "rate_cards": {
                "standard": {
                    "prices": {
                        "completion": [
                            {"match": {"model": {"prefix": "gpt-"}}, "formula": "input_tokens * 2"},
                            {"default": True, "formula": "input_tokens + output_tokens"},
                        ]
                    }
                }
            },
        },
        "credits": {
            "buckets": {"gifted": {"expires_after": {"unit": "day", "count": 7}}, "purchased": {}},
            "spend_order": ["gifted", "purchased"],
            "default_bucket": "purchased",
            "overdraft_bucket": "purchased",
        },
        "plans": {"pro": {"display_name": "Pro", "rate_card": "standard"}},
    }


def test_accepts_generic_operation_schema() -> None:
    config = load_config_from_dict(base_config())
    assert config.usage is not None
    assert config.usage.operations["completion"].measures == ["input_tokens", "output_tokens"]


def test_canonicalizes_decimals_without_legacy_sections() -> None:
    data = base_config()
    data["credits"]["signup_grant"] = {"amount": Decimal("10.25"), "bucket": "gifted"}
    canonical = canonical_bursar_config_dict(data)
    assert canonical["credits"]["signup_grant"]["amount"] == "10.25"
    assert "metering" not in canonical and "ledger" not in canonical and "billing" not in canonical


@pytest.mark.parametrize("legacy", [{"metering": {"models": {"*": "1"}}}, {"ledger": {}}, {"billing": {}}])
def test_rejects_legacy_sections(legacy: dict) -> None:
    with pytest.raises(ConfigError, match="Extra inputs"):
        load_config_from_dict({"version": 1, **legacy})


def test_rejects_unpriced_operation() -> None:
    data = base_config()
    data["usage"]["operations"]["image"] = {"measures": ["images"], "dimensions": []}
    with pytest.raises(ConfigError, match="no price"):
        load_config_from_dict(data)


def test_rejects_default_not_last() -> None:
    data = base_config()
    data["usage"]["rate_cards"]["standard"]["prices"]["completion"] = [
        {"default": True, "formula": "input_tokens"},
        {"match": {"model": {"exact": "x"}}, "formula": "input_tokens"},
    ]
    with pytest.raises(ConfigError, match="default rule"):
        load_config_from_dict(data)


def test_rejects_unknown_formula_measure() -> None:
    data = base_config()
    data["usage"]["rate_cards"]["standard"]["prices"]["completion"][-1]["formula"] = "unknown * 2"
    with pytest.raises(ConfigError, match="invalid formula"):
        load_config_from_dict(data)


def test_bucket_order_is_explicit() -> None:
    data = base_config()
    data["credits"]["spend_order"] = ["purchased"]
    with pytest.raises(ConfigError, match="every bucket exactly once"):
        load_config_from_dict(data)


def test_overdraft_requires_destination_bucket() -> None:
    data = base_config()
    data["credits"].pop("overdraft_bucket")
    data["plans"]["pro"]["spending"] = {"mode": "overdraft", "overdraft_limit": 10}
    with pytest.raises(ConfigError, match="overdraft_bucket"):
        load_config_from_dict(data)


def test_subscription_credit_stacking_is_explicit() -> None:
    data = base_config()
    data["plans"]["pro"]["included_credits"] = {"amount": 10, "reset": {"unit": "month"}}
    data["payments"] = {
        "subscriptions": {
            "pro_monthly": {
                "plan": "pro",
                "billing_period": {"unit": "month"},
                "providers": {"stripe": {"lookup": {"type": "price_id", "value": "price_pro"}}},
                "renewal_credits": {
                    "amount": 20,
                    "bucket": "purchased",
                    "behavior": "replace",
                    "on_subscription_end": "expire",
                },
            }
        }
    }
    with pytest.raises(ConfigError, match="stack_credits"):
        load_config_from_dict(data)


def test_auto_recharge_has_one_shape() -> None:
    data = base_config()
    data["payments"] = {
        "topups": {
            "small_pack": {
                "credits": 100,
                "bucket": "purchased",
                "providers": {"stripe": {"lookup": {"type": "price_id", "value": "price_pack"}}},
            }
        },
        "auto_recharge": {
            "trigger": {"balance_below": 10},
            "purchase": {"topup": "small_pack"},
            "limit": {"max_purchases": 3, "period": {"unit": "day", "count": 30, "anchor": "rolling"}},
        },
    }
    assert load_config_from_dict(data).payments is not None
