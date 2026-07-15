"""Tests for SQL hardening and catalog lifecycle behaviors."""

from decimal import Decimal

import pytest

from bursar.config import ConfigError, load_config_from_dict


class TestConfigHardening:
    def test_signup_grant_scalar_rejected(self) -> None:
        with pytest.raises(ConfigError, match="object"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"signup_grant": 50},
                }
            )

    def test_signup_grant_requires_bucket_in_ledger(self) -> None:
        with pytest.raises(ConfigError, match="unknown bucket"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "signup_grant": {"amount": 10, "bucket": "promo"},
                        "buckets": {"gifted": {"label": "Gifted", "priority": 10}},
                    },
                }
            )

    def test_rate_override_unknown_model_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown model"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {
                        "pro": {
                            "label": "Pro",
                            "rate_overrides": {"gpt-4": "input_tokens * 0.01"},
                        }
                    },
                }
            )

    def test_dangling_billing_plan_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown plan"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {"pro": {"label": "Pro"}},
                    "billing": {
                        "subscriptions": {"pro-monthly": {"plan": "enterprise", "grant": {"mode": "allowance"}}}
                    },
                }
            )

    def test_config_error_is_value_error(self) -> None:
        assert issubclass(ConfigError, ValueError)


class TestEngineRateOverrides:
    def test_plan_rate_override_applied(self) -> None:
        from bursar.engine import PricingEngine
        from bursar.metrics import UsageMetrics

        engine = PricingEngine.from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
            }
        )
        result = engine.calculate(
            UsageMetrics(model="*", input_tokens=10),
            rate_overrides={"*": "input_tokens * 5"},
        )
        assert result.total == Decimal("50.0000")
