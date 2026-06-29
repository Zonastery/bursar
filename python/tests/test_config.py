"""Tests for pricing config parsing and validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ducto.config import (
    ConfigError,
    PricingConfig,
    load_config_from_dict,
)
from ducto.interface.models import PricingConfigData


class TestConfigValidation:
    """Tests for config loading and validation."""

    def test_valid_full_config(self) -> None:
        """Loading a full config dict populates all sections."""
        config = load_config_from_dict(
            {
                "models": {"gpt-4": "input_tokens * 0.01"},
                "tools": {"_default": "tool_calls * 0.1"},
            }
        )
        assert config.models["gpt-4"] == "input_tokens * 0.01"

    def test_minimal_config(self) -> None:
        """Minimal config with only version and models works."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 0.001"},
            }
        )
        assert config.models["_default"] == "input_tokens * 0.001"

    def test_invalid_expression_raises_error(self) -> None:
        """An expression with disallowed syntax raises ConfigError."""
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "models": {"gpt-4": "lambda x: x"},
                }
            )

    def test_missing_models_raises_error(self) -> None:
        """Missing models section raises ConfigError."""
        with pytest.raises(ConfigError, match="models"):
            load_config_from_dict({})

    def test_negative_fixed_cost_raises_error(self) -> None:
        """Negative fixed cost values raise pydantic ValidationError."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "fixed": {"bad_job": -5},
                }
            )

    def test_tool_specific_costs(self) -> None:
        """Tool-specific expression strings are stored correctly."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {"_default": "tool_calls * 0", "web_search": "web_search_calls * 2"},
            }
        )
        assert config.tools["web_search"] == "web_search_calls * 2"

    def test_fixed_costs_are_positive(self) -> None:
        """Positive fixed cost values are accepted."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"batch_job": 20, "slow_job": 10},
            }
        )
        assert config.fixed["batch_job"] == 20

    def test_min_balance_is_decimal(self) -> None:
        """min_balance is a Decimal money field (contract §1)."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 10})
        assert config.min_balance == Decimal(10)
        assert isinstance(config.min_balance, Decimal)

    def test_min_balance_default_is_decimal(self) -> None:
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}})
        assert config.min_balance == Decimal(5)
        assert isinstance(config.min_balance, Decimal)

    def test_negative_min_balance_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": -1})

    def test_fractional_min_balance(self) -> None:
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": "2.5"})
        assert config.min_balance == Decimal("2.5")

    def test_unknown_variable_rejected_at_config_load(self) -> None:
        """A typo'd metric variable fails at config-load, not runtime (M5)."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict({"models": {"_default": "inputtokens * 0.001"}})

    def test_known_variables_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
                "search": {"costs": "search_queries * 0.5 + search_results * 0.05"},
                "cache": {"discount": "-cache_read_tokens * 0.0045"},
            }
        )
        assert config.models["_default"]

    def test_pow_expression_rejected_at_config_load(self) -> None:
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict({"models": {"_default": "input_tokens ** 2"}})

    def test_plan_missing_name_raises_config_error(self) -> None:
        """A plan dict without 'name' raises ConfigError, not KeyError."""
        with pytest.raises(ConfigError, match="missing required 'name'"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {"pro": {"id": "pro", "free_allowance": 100}},
                }
            )

    def test_duplicate_plan_names_raises(self) -> None:
        with pytest.raises(ConfigError, match="duplicate plan names"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {
                        "a": {"id": "a", "name": "Pro"},
                        "b": {"id": "b", "name": "Pro"},
                    },
                }
            )

    def test_pricing_config_field_alignment(self) -> None:
        """PricingConfig and PricingConfigData fields stay in sync.

        Prevents silent data loss when fields are added to one model but not the other.
        The validated config (PricingConfig) and raw data model (PricingConfigData)
        must share the same set of fields for reliable round-trip through stores.
        """
        config_fields = set(PricingConfig.model_fields.keys())
        data_fields = set(PricingConfigData.model_fields.keys())
        assert config_fields == data_fields, (
            f"Field drift: PricingConfig has {config_fields - data_fields}, "
            f"PricingConfigData has {data_fields - config_fields}"
        )

    # ── CF1: Plan rate_overrides accepted ──────────────────────────────────

    def test_plan_rate_overrides_accepted(self) -> None:
        """CF1 — A plan with rate_overrides is loaded without error."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {
                    "pro": {
                        "id": "pro",
                        "name": "Pro",
                        "rate_overrides": {"gpt-4": "input_tokens * 0.003"},
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["pro"]
        assert plan.rate_overrides == {"gpt-4": "input_tokens * 0.003"}

    # ── CF2: Plan free_allowance negative is rejected ──────────────────────

    def test_plan_negative_free_allowance_rejected(self) -> None:
        """CF2 — free_allowance: -10 in a plan raises a validation error."""
        with pytest.raises((ConfigError, ValidationError)):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {
                        "cheap": {
                            "id": "cheap",
                            "name": "Cheap",
                            "free_allowance": -10,
                        }
                    },
                }
            )

    # ── CF3: Version field — only 1 is valid ──────────────────────────────

    def test_version_2_rejected(self) -> None:
        """CF3 — config with version: 2 raises a validation error (Literal[1])."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 2,
                    "models": {"_default": "input_tokens * 1"},
                }
            )

    # ── CF4: Empty sections are allowed ────────────────────────────────────

    def test_empty_sections_allowed(self) -> None:
        """CF4 — Empty tools/search/cache/fixed sections are valid when models is present."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {},
                "search": {},
                "cache": {},
                "fixed": {},
            }
        )
        assert config.tools == {}
        assert config.search == {}
        assert config.cache == {}
        assert config.fixed == {}

    # ── CF5: Plan with features: null ──────────────────────────────────────

    def test_plan_features_null_is_valid(self) -> None:
        """CF5 — A plan with features: null is valid and returns empty features dict."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {
                    "basic": {
                        "id": "basic",
                        "name": "Basic",
                        "features": None,
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["basic"]
        assert plan.features is None

        # Verify that get_user_plan returns empty features (not None) for such a plan.
        from ducto import MemoryStore
        from ducto.interface.models import PlanDefinition, PricingConfigData

        store = MemoryStore()
        store.set_active_pricing(
            PricingConfigData(
                models={"_default": "1"},
                plans={"basic": PlanDefinition(id="basic", name="Basic", features=None)},
            )
        )
        store.set_user_plan("user-1", "basic")
        result = store.get_user_plan("user-1")
        assert result.features == {}

    # ── CF6: Duplicate plan names rejected ─────────────────────────────────
    # (already covered by test_duplicate_plan_names_raises above)

    # ── CF7: min_balance string coerces to Decimal ─────────────────────────

    def test_min_balance_string_coerces_to_decimal(self) -> None:
        """CF7 — min_balance: '10' (string) is coerced to Decimal('10') without error."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "min_balance": "10",
            }
        )
        assert config.min_balance == Decimal("10")
        assert isinstance(config.min_balance, Decimal)
